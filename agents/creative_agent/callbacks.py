import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from google.adk.agents.callback_context import CallbackContext
from google.adk.sessions.state import State
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from agent_common import observability, sanitize
from agent_common.rate_limit import build_rate_limit_callback
from agent_common.state import seed_initial_state

from .citations import render_citations
from .concept_guard import (
    concept_issues,
    enforce_person_casting,
    enforce_timely_cap,
    ensure_trend_and_product,
    flatten_concept_issues,
    parse_concepts,
    restore_unflagged_concepts,
)
from .config import config
from .copy_gate import parse_copies, restore_unflagged
from .person_render import person_reference_available
from .prompts import PERSON_CASTING_RULES
from .rating_signals import strictness_flags
from .references import reference_roles_summary, resolve_references
from .safer_angle import derive_state as derive_safer_angle_state
from .style_shortlist import format_shortlist, pick_style_shortlist
from .trend_situation import timely_allowed

# Shared debugging-observability callbacks (agent_common, WS3). Re-exported here
# so creative_agent/agent.py references `callbacks.<name>`, matching trend_scout.
log_empty_turn_finish_reason = observability.log_empty_turn_finish_reason
scrub_surrogates_in_response = sanitize.scrub_surrogates_in_response
log_final_state_summary = observability.make_final_state_summary(
    "creative_agent",
    (
        "combined_final_cited_report",
        "creative_brief",
        "ad_copy_critique",
        "final_visual_concepts",
        "creative_evaluation_report",
    ),
)

logger = logging.getLogger(__name__)


def _apply_safer_angle(target: State | dict[str, Any]) -> None:
    """Opt-in safer angle for a sensitive trend (creative_agent/safer_angle.py).

    Only ``safer_angle_opt_in`` is a client input (the campaign form seeds it;
    ``setdefault(False)``, only exactly True counts). Every derived key — the
    trend_scout flag, ``safer_angle_active`` (opt-in AND flag), the reason and
    the four ``safer_angle_*_rules`` texts — is ASSIGNED from
    ``derive_state`` (opt-in + ``trend_scout_context``), never setdefault'ed,
    so a client cannot seed an active state or inject rule text. The rule
    texts reach the prompts as optional {safer_angle_*_rules?} tokens and are
    "" otherwise, so runs without the opt-in get unchanged instructions.
    """
    target.setdefault("safer_angle_opt_in", False)
    inputs = {
        key: target.get(key) for key in ("safer_angle_opt_in", "trend_scout_context")
    }
    for key, value in derive_safer_angle_state(inputs).items():
        if key != "safer_angle_opt_in":
            target[key] = value


def _set_initial_states(source: dict[str, Any], target: State | dict[str, Any]):
    """
    Setting the initial session state given a JSON object of states.

    Args:
        source: A JSON object of states.
        target: The session state object to insert into.
    """
    seeded = seed_initial_state(
        source,
        target,
        state_init_key=config.state_init,
        gcs_bucket=config.GCS_BUCKET,
        agent_output_dir="creative_output",
        extra={"gcs_bucket_name": config.GCS_BUCKET_NAME},
        timezone=config.trend_timezone,
    )
    # Recomputed on every call, seeded or not (deterministic, so a resumed
    # session gets the same values); see _apply_safer_angle.
    _apply_safer_angle(target)
    if not seeded:
        return

    # Core campaign fields. Callers (frontend, CRF worker) seed them via
    # createSession initialState so the inputs are deterministic rather than
    # parsed from the kickoff message; the root's `memorize` step only fills
    # the ones still missing. setdefault so the keys always exist for the
    # {brand} etc. prompt tokens WITHOUT clobbering a seeded value.
    # Deliberately NOT in `source` (which would blank a seeded value).
    for _campaign_key in (
        "brand",
        "target_product",
        "target_audience",
        "key_selling_points",
        "target_search_trends",
    ):
        target.setdefault(_campaign_key, "")

    # trend_scout's detection date + compact research context for this trend
    # (CRF worker: refresh_date / trend_context; frontend: the trend card).
    # Read only as hints to re-verify ({trend_scout_context?} /
    # {trend_detected_on?}); same seed + setdefault rule as the campaign fields.
    for _trend_hint_key in ("trend_detected_on", "trend_scout_context"):
        target.setdefault(_trend_hint_key, "")

    # Optional product/brand reference image for image generation, supplied
    # by the caller via createSession initialState (same mechanism as
    # interactive_trend_pick). Use setdefault so the key always exists for
    # downstream .get() reads WITHOUT clobbering a caller-provided value —
    # it is deliberately NOT in `source` above, which would overwrite it.
    target.setdefault("reference_image_uri", "")

    # Optional user-supplied visual intent (image-intent-capture). Same
    # channel + rule as reference_image_uri: seeded via createSession
    # initialState, defaulted here with setdefault so the keys always exist
    # for downstream {token?} reads / .get() without clobbering caller values.
    # Deliberately NOT in `source` (which would blank a seeded value).
    for _intent_key in (
        "visual_intent",  # free-text art direction
        "brand_colors",  # palette description
        "visual_style_preference",  # preferred STYLE_PALETTE family (seed)
        "visual_avoid",  # elements to keep out (reframed positively)
        "visual_aspect_ratio",  # deterministic aspect-ratio override
        "reference_image_role",  # product | logo | style role label
    ):
        target.setdefault(_intent_key, "")

    # Optional multiple reference images (`[{"uri", "role"}]`, max 4), same
    # channel + setdefault rule. `reference_roles` ("product, style") is the
    # ordered role list of the resolved references (legacy single reference
    # folded in first) for the drafter's {reference_roles?} token.
    target.setdefault("reference_images", [])
    # Opt-in rating learning (per run, default off): the campaign form's "Learn
    # from past ratings for this brand" seeds True via initialState; only
    # exactly True turns it on (creative_agent/rating_signals.py).
    target.setdefault("learn_from_ratings", False)
    target.setdefault("reference_roles", reference_roles_summary(target))
    target.setdefault("reference_product_description", "")
    # Optional consented person reference ({uri, consent_id}; consent-checked by
    # the api at kick-off). Kept out of reference_images on purpose. The visual
    # agents see only the derived yes/"" flag, never the URI.
    target.setdefault("person_reference", {})
    target.setdefault(
        "person_reference_available",
        person_reference_available(target.get("person_reference")),
    )
    # The casting rules reach the visual agents only on runs with a person
    # ({person_casting_rules?}); without one their instructions are unchanged.
    target.setdefault(
        "person_casting_rules",
        PERSON_CASTING_RULES if target.get("person_reference_available") else "",
    )

    # The timely slot's rules reach the creative agents only when the trend
    # situation validator found a timely moment ({timely_rules?}, written by
    # validate_trend_situation); otherwise their instructions are unchanged.
    target.setdefault("timely_rules", "")
    # Opt-in rating learning's stricter limits reach the critics, revisers and
    # visual producers only when the checks effect flagged something
    # ({rating_limits?}, written by load_brand_history next to
    # rating_strictness); otherwise their instructions are unchanged.
    target.setdefault("rating_limits", "")

    # Per-session random style shortlist (image diversity): the visual agents
    # pick their 4 styles from it, so runs don't converge on the same looks.
    # setdefault: a caller/test-provided shortlist (or a resumed session) wins.
    target.setdefault("style_shortlist", format_shortlist(pick_style_shortlist()))

    # Optional inbound Discovery Engine enterprise grounding state. Respect any
    # caller-seeded `enterprise_grounding_enabled` boolean from initialState.
    target.setdefault("enterprise_research_findings", "")
    target.setdefault("enterprise_sources", {})
    target.setdefault("url_to_ent_id", {})
    target.setdefault("enterprise_grounding_enabled", True)

    # Opt-in Phase 3 multi-cut commercial video production (default OFF so
    # standard Phase 2 runs incur zero video latency or cost).
    target.setdefault("video_production_enabled", False)


def load_session_state(callback_context: CallbackContext):
    """
    Sets up the initial state.
    Set this as a callback as before_agent_call of the `root_agent`.
    This gets called before the system instruction is constructed.

    Args:
        callback_context: The callback context.
    """
    observability.log_run_start(callback_context)

    _set_initial_states({}, callback_context.state)


def reset_brief_state(callback_context: CallbackContext) -> None:
    """`before_agent_callback` on `brief_writer`: start each research run clean.

    Clears a previous run's `creative_brief` (else `brief_writer_resilient` would
    count the stale value as populated — the RetryUntilKeyNode "earlier turn"
    limitation), its compact Markdown rendering (`creative_brief_md`), the
    gate's revision counter and feedback, and the residual-issue and exhaustion
    markers, so a re-run research pipeline in the same session gets a fresh brief
    and a fresh revision budget. Idempotent (runs once per retry attempt).
    Returns None so the agent runs normally.
    """
    state = callback_context.state
    state["creative_brief"] = None
    state["creative_brief_md"] = ""
    state["brief_issues"] = ""
    state["brief_revision_rounds_used"] = 0
    state["creative_brief__issues"] = None
    state["creative_brief__retry_exhausted"] = None
    return None


# State the copy gate's bounded revision loop owns (see agent.copy_gate).
COPY_REVISION_STATE_DEFAULTS: dict[str, Any] = {
    "ad_copy_issues": "",
    "ad_copy_flagged_ids": None,
    "ad_copy_critique__before_revision": None,
    "ad_copy_revision_rounds_used": 0,
    "ad_copy_critique__issues": None,
}


def reset_copy_state(callback_context: CallbackContext) -> None:
    """`before_agent_callback` on `ad_copy_drafter`: a fresh copy-revision loop.

    Clears a previous ad-copy run's gate feedback (`ad_copy_issues`, flagged
    ids, pre-revision snapshot), revision counter and residual-issue marker, so
    a re-run ad_creative_pipeline in the same session gets a fresh revision
    budget and no stale warning. Returns None so the agent runs normally.

    Deliberately does NOT touch `ad_copy_feedback`: it is user input (interactive
    checkpoint 2), not gate state; its owner sets/clears it (see the
    ad_copy_reviser note in agent.py).
    """
    for key, value in COPY_REVISION_STATE_DEFAULTS.items():
        callback_context.state[key] = value
    return None


def restore_unflagged_copies_callback(callback_context: CallbackContext) -> None:
    """`after_agent_callback` on `ad_copy_reviser`: only flagged copies change.

    copy_gate snapshots the pre-revision critique
    (`ad_copy_critique__before_revision`) and the flagged copy ids
    (`ad_copy_flagged_ids`) before routing to the reviser. Any copy the reviser
    changed without being flagged, dropped, duplicated or invented is put back
    (pure logic in `copy_gate.restore_unflagged`), with a warning per
    intervention. Runs after the reviser's output_key write, so the repaired
    value lands as a later state delta.
    """
    state = callback_context.state
    before = state.get("ad_copy_critique__before_revision")
    if not parse_copies(before):
        return None
    flagged = state.get("ad_copy_flagged_ids") or []
    restored, notes = restore_unflagged(before, state.get("ad_copy_critique"), flagged)
    if notes:
        logger.warning("ad_copy_reviser output repaired: %s", "; ".join(notes))
        state["ad_copy_critique"] = restored
    return None


# State the concept gate's bounded fix loop owns (see agent.concept_gate).
CONCEPT_REVISION_STATE_DEFAULTS: dict[str, Any] = {
    "visual_concept_issues": "",
    "visual_concept_flagged_ids": None,
    "final_visual_concepts__before_revision": None,
    "visual_concept_revision_rounds_used": 0,
    "final_visual_concepts__issues": None,
}


def reset_concept_state(callback_context: CallbackContext) -> None:
    """`before_agent_callback` on `art_director`: a fresh concept-fix loop.

    Clears a previous visual run's gate feedback (`visual_concept_issues`,
    flagged ids, pre-revision snapshot), fix counter and residual-issue marker,
    so a re-run visual_generation_pipeline in the same session gets a fresh fix
    budget and no stale warning. Returns None so the agent runs normally.
    """
    for key, value in CONCEPT_REVISION_STATE_DEFAULTS.items():
        callback_context.state[key] = value
    return None


def _restore_unflagged_concepts(
    state: Any, *, before_key: str, flagged_key: str, producer: str
) -> None:
    """Put back every concept the producer changed without being flagged.

    ``state[before_key]`` is the pre-revision snapshot and ``state[flagged_key]``
    the flagged concept keys (``concept_guard.concept_keys``). Without a usable
    snapshot nothing happens (an older session, or no revision prepared).
    """
    before = state.get(before_key)
    if not parse_concepts(before):
        return
    flagged = state.get(flagged_key) or []
    restored, notes = restore_unflagged_concepts(
        before, state.get("final_visual_concepts"), flagged
    )
    if notes:
        logger.warning("%s output repaired: %s", producer, "; ".join(notes))
        state["final_visual_concepts"] = restored


def restore_unflagged_concepts_callback(callback_context: CallbackContext) -> None:
    """`after_agent_callback` on `visual_concept_fixer`: only flagged concepts change.

    concept_gate snapshots the pre-revision concepts
    (`final_visual_concepts__before_revision`) and the flagged concept keys
    (`visual_concept_flagged_ids`) before routing to the fixer. Any concept the
    fixer changed without being flagged, dropped, duplicated or invented is put
    back (pure logic in `concept_guard.restore_unflagged_concepts`), with a
    warning per intervention. Runs after the fixer's output_key write and
    BEFORE `ensure_trend_and_product_callback` (so a fixed concept is guarded).
    """
    _restore_unflagged_concepts(
        callback_context.state,
        before_key="final_visual_concepts__before_revision",
        flagged_key="visual_concept_flagged_ids",
        producer="visual_concept_fixer",
    )
    return None


def restore_revision_unflagged_concepts(callback_context: CallbackContext) -> None:
    """`after_agent_callback` on interactive's `visual_concept_reviser` (first):
    only the concepts the checkpoint-3 notes name change.

    The resume (`runserver.async_runs.visual_concept_edits_delta`) records the
    concepts after the user's direct edits (`visual_revision_snapshot`) and the
    keys of the concepts with a note (`visual_revision_flagged_ids`). Like the
    fixer's restore, any other concept the reviser changed (e.g. paraphrased a
    direct edit), renamed, dropped, duplicated or invented is put back from the
    snapshot, and a noted concept keeps its `concept_name` (its image key),
    BEFORE `ensure_trend_and_product_callback`. Sessions without the
    snapshot (resumed before it existed) are left as the reviser wrote them.
    """
    state = callback_context.state
    _restore_unflagged_concepts(
        state,
        before_key="visual_revision_snapshot",
        flagged_key="visual_revision_flagged_ids",
        producer="visual_concept_reviser",
    )
    # A noted concept keeps its name too: the name is its artifact / image key
    # (a note revises the image prompt, never the concept's identity).
    old = parse_concepts(state.get("visual_revision_snapshot"))
    new = parse_concepts(state.get("final_visual_concepts"))
    if old and len(old) == len(new):
        renamed = [
            {**n, "concept_name": o["concept_name"]}
            if "concept_name" in o and n.get("concept_name") != o["concept_name"]
            else dict(n)
            for o, n in zip(old, new, strict=True)
        ]
        if renamed != new:
            logger.warning("visual_concept_reviser output repaired: kept names")
            state["final_visual_concepts"] = {"visual_concepts": renamed}
    return None


def ensure_trend_and_product_callback(callback_context: CallbackContext) -> None:
    """`after_agent_callback` guaranteeing every final image prompt shows the
    trend motif, the product and the concept's brand cue (image diversity,
    Task 4b).

    Wired on `visual_concept_finalizer`, `visual_concept_fixer` (after its
    restore callback) and interactive's `visual_concept_reviser`, the producers
    of `final_visual_concepts`. ADK runs after_agent_callback
    once the agent's output event (carrying the `output_key` state delta) has been
    yielded, so the LLM's concepts are readable here; the repaired value is written
    back to the same key in the same shape (dict, or a JSON string of one) and
    lands as a later state delta, so `generate_image` reads the repaired prompts.
    Returns None: returned content would replace the agent's output. Writes only
    when a prompt actually changed; logs each repair/miss as a warning.
    """
    state = callback_context.state
    raw = state.get("final_visual_concepts")
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return None
    else:
        value = raw
    if not isinstance(value, dict):
        return None
    concepts = value.get("visual_concepts")
    if not isinstance(concepts, list) or not concepts:
        return None
    if not all(isinstance(c, dict) for c in concepts):
        return None

    # Person casting first, so a cleared cast's prompt is then guarded as usual.
    cast_checked, cast_warnings = enforce_person_casting(
        concepts,
        available=bool(person_reference_available(state.get("person_reference"))),
        max_cast=config.max_cast_concepts,
        safe_styles=config.person_safe_styles,
    )
    for warning in cast_warnings:
        logger.warning(f"casting guard: {warning}")
    # Then the timely cap (first MAX_TIMELY_CONCEPTS flags kept; none without
    # a validated timely moment).
    timely_checked, timely_warnings = enforce_timely_cap(
        cast_checked,
        timely_ok=timely_allowed(state, config.max_timely_concepts),
        max_timely=config.max_timely_concepts,
    )
    for warning in timely_warnings:
        logger.warning(f"timely guard: {warning}")
    repaired, warnings = ensure_trend_and_product(
        timely_checked,
        str(state.get("target_product") or ""),
        brand=str(state.get("brand") or ""),
        strictness=strictness_flags(state.get("rating_strictness")),
    )
    for warning in warnings:
        logger.warning(f"concept guard: {warning}")
    if repaired == concepts:
        return None
    new_value = {**value, "visual_concepts": repaired}
    state["final_visual_concepts"] = (
        json.dumps(new_value) if isinstance(raw, str) else new_value
    )
    return None


def recheck_concept_issues_callback(callback_context: CallbackContext) -> None:
    """`after_agent_callback` on interactive's `visual_concept_reviser` (after
    `ensure_trend_and_product_callback`): re-run the deterministic concept checks.

    concept_gate ran BEFORE checkpoint 3; the user's edits and the reviser can
    fix or introduce issues afterwards, so its `final_visual_concepts__issues`
    verdict may be stale. This recomputes it on the current concepts (with
    `ad_copy_critique`, `brand`, `target_product`) — flattened issues, or None
    when clean or absent. Record only: there is no fix loop after the human
    checkpoint. Returns None so the agent's output is kept.
    """
    state = callback_context.state
    concepts = state.get("final_visual_concepts")
    roles = state.get("reference_roles") or [
        role for _, role in resolve_references(state)
    ]
    issues = concept_issues(
        concepts,
        state.get("ad_copy_critique"),
        brand=str(state.get("brand") or ""),
        target_product=str(state.get("target_product") or ""),
        strictness=strictness_flags(state.get("rating_strictness")),
        timely_ok=timely_allowed(state, config.max_timely_concepts),
        reference_roles=roles,
        visual_style_preference=str(state.get("visual_style_preference") or ""),
    )
    residual = flatten_concept_issues(concepts, issues) or None
    if residual:
        logger.warning("visual concept issues after revision: %s", residual)
    state["final_visual_concepts__issues"] = residual
    return None


# State the storyboard gate's bounded revision loop owns (see video_stage.storyboard_gate).
VIDEO_STORYBOARD_STATE_DEFAULTS: dict[str, Any] = {
    "video_storyboard": None,
    "video_storyboard_issues": "",
    "video_storyboard_revision_rounds_used": 0,
    "video_storyboard__issues": None,
    "video_storyboard__retry_exhausted": None,
    "_videos_generated": False,
    "generated_videos": None,
    "generated_videos__issues": None,
    "video_qa__issues": None,
}


def reset_video_state(callback_context: CallbackContext) -> None:
    """`before_agent_callback` on `video_director`: start each storyboard run clean."""
    for key, value in VIDEO_STORYBOARD_STATE_DEFAULTS.items():
        callback_context.state[key] = value
    return None


# Shared query rate limiter (agent_common). Built with creative_agent's config so
# `callbacks.rate_limit_callback` keeps the same name/signature for the
# before_model_callback wiring in agent.py (and interactive_creative reuse).
rate_limit_callback = build_rate_limit_callback(config)


def collect_research_sources_callback(callback_context: CallbackContext) -> None:
    """Collects and organizes web-based research sources and their supported claims from agent events.

    This function processes the agent's `session.events` to extract web source details (URLs,
    titles, domains from `grounding_chunks`) and associated text segments with confidence scores
    (from `grounding_supports`). The aggregated source information and a mapping of URLs to short
    IDs are cumulatively stored in `callback_context.state`. The whole session is re-scanned on
    every call (ids stay stable); a claim already recorded for a source is not appended again.

    Args:
        callback_context (CallbackContext): The context object providing access to the agent's
            session events and persistent state.
    """
    session = callback_context.session
    url_to_short_id = callback_context.state.get("url_to_short_id", {})
    sources = callback_context.state.get("sources", {})
    id_counter = len(url_to_short_id) + 1
    agent_name = getattr(callback_context, "agent_name", None)
    invocation_id = getattr(callback_context, "invocation_id", None)
    own_ids: list[str] = []
    for event in session.events:
        if not (event.grounding_metadata and event.grounding_metadata.grounding_chunks):
            continue
        is_own = (
            agent_name is not None
            and getattr(event, "author", None) == agent_name
            and getattr(event, "invocation_id", None) == invocation_id
        )
        chunks_info = {}
        for idx, chunk in enumerate(event.grounding_metadata.grounding_chunks):
            if not chunk.web:
                continue
            url = chunk.web.uri
            title = (
                chunk.web.title
                if chunk.web.title != chunk.web.domain
                else chunk.web.domain
            )
            if url not in url_to_short_id:
                short_id = f"src-{id_counter}"
                url_to_short_id[url] = short_id
                sources[short_id] = {
                    "short_id": short_id,
                    "title": title,
                    "url": url,
                    "domain": chunk.web.domain,
                    "supported_claims": [],
                }
                id_counter += 1
            chunks_info[idx] = url_to_short_id[url]
            if is_own and url_to_short_id[url] not in own_ids:
                own_ids.append(url_to_short_id[url])
        if event.grounding_metadata.grounding_supports:
            for support in event.grounding_metadata.grounding_supports:
                confidence_scores = support.confidence_scores or []
                chunk_indices = support.grounding_chunk_indices or []
                for i, chunk_idx in enumerate(chunk_indices):
                    if chunk_idx in chunks_info:
                        short_id = chunks_info[chunk_idx]
                        confidence = (
                            confidence_scores[i] if i < len(confidence_scores) else 0.5
                        )
                        text_segment = support.segment.text if support.segment else ""
                        claims = sources[short_id]["supported_claims"]
                        # Every searcher re-scans the whole session, so a claim
                        # seen on an earlier pass is kept once (highest score).
                        known = next(
                            (c for c in claims if c["text_segment"] == text_segment),
                            None,
                        )
                        if known is None:
                            claims.append(
                                {
                                    "text_segment": text_segment,
                                    "confidence": confidence,
                                }
                            )
                        elif confidence > known["confidence"]:
                            known["confidence"] = confidence
    callback_context.state["url_to_short_id"] = url_to_short_id
    callback_context.state["sources"] = sources
    if agent_name is not None:
        by_agent = dict(callback_context.state.get("source_ids_by_agent") or {})
        by_agent[agent_name] = own_ids
        callback_context.state["source_ids_by_agent"] = by_agent


def collect_trend_situation_sources_callback(
    callback_context: CallbackContext,
) -> None:
    """`after_agent_callback` on `trend_situation_searcher`.

    Runs `collect_research_sources_callback` (every source still joins
    ``sources`` with its ``src-N`` id, where the validator checks the cited ids),
    then writes ``trend_situation_sources``: only the sources this searcher's own
    turns grounded on in this invocation, as compact JSON keyed by id, for the
    synthesizer's ``{trend_situation_sources?}`` token. The whole ``sources``
    dict would carry the other research branches' snippets, and its size would
    depend on which branch finished first. ``""`` when the searcher grounded on
    nothing.
    """
    collect_research_sources_callback(callback_context)
    state = callback_context.state
    agent_name = getattr(callback_context, "agent_name", None)
    own_ids = (state.get("source_ids_by_agent") or {}).get(agent_name) or []
    sources = state.get("sources") or {}
    scoped = {sid: sources[sid] for sid in own_ids if sid in sources}
    state["trend_situation_sources"] = (
        json.dumps(scoped, ensure_ascii=False) if scoped else ""
    )


def _extract_enterprise_domain(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https", "gs") and parsed.netloc:
        return parsed.netloc
    return ""


def _fallback_enterprise_title(
    url: str, domain: str, chunk_id: str, short_id: str
) -> str:
    if url:
        parsed = urlparse(url)
        if parsed.scheme == "gs":
            basename = parsed.path.rstrip("/").rsplit("/", 1)[-1].strip()
            if basename:
                return basename
    return domain or chunk_id or short_id


def collect_enterprise_sources_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    tool_response: Any,
) -> dict[str, Any] | None:
    """Collects and deduplicates Discovery Engine search results into ``ent-N`` sources.

    Reads ``{"status": "success", "results": [...]}`` payloads returned by the
    enterprise search tool, minting stable ``ent-1``, ``ent-2``, ... entries in
    ``tool_context.state["enterprise_sources"]`` deduplicated via
    ``tool_context.state["url_to_ent_id"]``.
    """
    if not isinstance(tool_response, Mapping):
        return None
    status = tool_response.get("status")
    if status == "error":
        err_msg = str(
            tool_response.get("error_message") or "Discovery Engine search failed"
        ).strip()
        errors = list(tool_context.state.get("_enterprise_search_errors") or [])
        errors.append(err_msg)
        tool_context.state["_enterprise_search_errors"] = errors
        return None
    if status != "success":
        return None
    tool_context.state["_enterprise_search_successes"] = (
        int(tool_context.state.get("_enterprise_search_successes") or 0) + 1
    )
    results = tool_response.get("results")
    if not isinstance(results, list):
        return None

    url_to_ent_id = dict(tool_context.state.get("url_to_ent_id") or {})
    enterprise_sources = dict(tool_context.state.get("enterprise_sources") or {})
    id_counter = len(enterprise_sources) + 1

    for item in results:
        if not isinstance(item, Mapping):
            continue
        raw_url = str(item.get("url") or item.get("uri") or "").strip()
        chunk_id = str(
            item.get("chunk_id") or item.get("document") or item.get("id") or ""
        ).strip()
        raw_title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or item.get("content") or "").strip()[:500]
        if not (raw_url or chunk_id or raw_title or snippet):
            continue

        if raw_url and chunk_id:
            dedupe_key = f"{raw_url}#{chunk_id}"
        elif raw_url:
            dedupe_key = raw_url
        elif chunk_id:
            dedupe_key = chunk_id
        else:
            dedupe_key = f"{raw_title}|{snippet[:120]}"

        if dedupe_key not in url_to_ent_id:
            short_id = f"ent-{id_counter}"
            domain = _extract_enterprise_domain(raw_url)
            title = raw_title or _fallback_enterprise_title(
                raw_url, domain, chunk_id, short_id
            )
            url_to_ent_id[dedupe_key] = short_id
            enterprise_sources[short_id] = {
                "short_id": short_id,
                "title": title,
                "url": raw_url,
                "domain": domain,
                "snippet": snippet,
                "chunk_id": chunk_id,
                "source_type": "enterprise",
            }
            id_counter += 1

    tool_context.state["url_to_ent_id"] = url_to_ent_id
    tool_context.state["enterprise_sources"] = enterprise_sources
    return None


def citation_replacement_callback(
    callback_context: CallbackContext,
) -> types.Content | None:
    """Replaces citation tags in a report with Markdown-formatted links.

    Processes 'combined_final_cited_report' from context state, converting tags like
    `<cite source="src-N"/>` and `<cite source="ent-N"/>` into hyperlinks or
    enterprise markers using source information from `callback_context.state`.

    Args:
        callback_context (CallbackContext): Contains the report and source information.

    Returns:
        types.Content: The processed report with Markdown citation links.
    """
    final_report = callback_context.state.get("combined_final_cited_report", "")
    sources = callback_context.state.get("sources", {})
    enterprise_sources = callback_context.state.get("enterprise_sources", {})
    callback_context.state["final_report_with_citations"] = render_citations(
        final_report,
        sources,
        enterprise_sources=enterprise_sources,
    )
    return types.Content(parts=[types.Part(text="Research report composed 📝")])
