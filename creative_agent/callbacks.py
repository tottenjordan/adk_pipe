import json
import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.sessions.state import State
from google.genai import types

from agent_common import observability, sanitize
from agent_common.rate_limit import build_rate_limit_callback
from agent_common.state import seed_initial_state

from .citations import render_citations
from .concept_guard import (
    concept_issues,
    ensure_trend_and_product,
    flatten_concept_issues,
    parse_concepts,
    restore_unflagged_concepts,
)
from .config import config
from .copy_gate import parse_copies, restore_unflagged
from .rating_signals import strictness_flags
from .references import reference_roles_summary
from .style_shortlist import format_shortlist, pick_style_shortlist

# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


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
    )
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

    # Optional multiple reference images (`[{"uri", "role"}]`, max 3), same
    # channel + setdefault rule. `reference_roles` ("product, style") is the
    # ordered role list of the resolved references (legacy single reference
    # folded in first) for the drafter's {reference_roles?} token.
    target.setdefault("reference_images", [])
    # Opt-in rating learning (per run, default off): the campaign form's "Learn
    # from past ratings for this brand" seeds True via initialState; only
    # exactly True turns it on (creative_agent/rating_signals.py).
    target.setdefault("learn_from_ratings", False)
    target.setdefault("reference_roles", reference_roles_summary(target))

    # Per-session random style shortlist (image diversity): the visual agents
    # pick their 4 styles from it, so runs don't converge on the same looks.
    # setdefault: a caller/test-provided shortlist (or a resumed session) wins.
    target.setdefault("style_shortlist", format_shortlist(pick_style_shortlist()))


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
        logging.warning("ad_copy_reviser output repaired: %s", "; ".join(notes))
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
    state = callback_context.state
    before = state.get("final_visual_concepts__before_revision")
    if not parse_concepts(before):
        return None
    flagged = state.get("visual_concept_flagged_ids") or []
    restored, notes = restore_unflagged_concepts(
        before, state.get("final_visual_concepts"), flagged
    )
    if notes:
        logging.warning("visual_concept_fixer output repaired: %s", "; ".join(notes))
        state["final_visual_concepts"] = restored
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

    repaired, warnings = ensure_trend_and_product(
        concepts,
        str(state.get("target_product") or ""),
        brand=str(state.get("brand") or ""),
        strictness=strictness_flags(state.get("rating_strictness")),
    )
    for warning in warnings:
        logging.warning(f"concept guard: {warning}")
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
    issues = concept_issues(
        concepts,
        state.get("ad_copy_critique"),
        brand=str(state.get("brand") or ""),
        target_product=str(state.get("target_product") or ""),
        strictness=strictness_flags(state.get("rating_strictness")),
    )
    residual = flatten_concept_issues(concepts, issues) or None
    if residual:
        logging.warning("visual concept issues after revision: %s", residual)
    state["final_visual_concepts__issues"] = residual
    return None


# Shared query rate limiter (agent_common). Built with creative_agent's config so
# `callbacks.rate_limit_callback` keeps the same name/signature for the
# before_model_callback wiring in agent.py (and interactive_creative reuse).
rate_limit_callback = build_rate_limit_callback(config)


def force_image_tool_call(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> None:
    """Constrain the visual_generator turn to actually call `generate_image`.

    Root-cause backstop for issue #116: visual_generator (gemini-3.1-pro-preview)
    intermittently returns MALFORMED_FUNCTION_CALL and emits NO tool call, leaving
    `_images_generated` unset and shipping an empty gallery. `RetryUntilKeyNode`
    (visual_generator_resilient) retries that, but each retry is still a free-choice
    turn that can flake again. Setting `tool_config` mode=ANY with
    `allowed_function_names=["generate_image"]` forces the model to predict that one
    function call every turn, making the common path deterministic rather than
    probabilistic (the retry cap remains as defense-in-depth).

    Gated on `_images_generated`: `generate_image` is idempotent (its guard sets that
    flag on success), and the resilient wrapper may re-enter after success. Once the
    images exist we must NOT re-force a tool call — leave the turn free so the agent
    can simply finish. Wired as a `before_model_callback` on `visual_generator`
    AFTER `rate_limit_callback` (both return None, so both run in order).
    """
    if callback_context.state.get("_images_generated"):
        return None
    llm_request.config.tool_config = types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(
            mode=types.FunctionCallingConfigMode.ANY,
            allowed_function_names=["generate_image"],
        )
    )
    return None


def collect_research_sources_callback(callback_context: CallbackContext) -> None:
    """Collects and organizes web-based research sources and their supported claims from agent events.

    This function processes the agent's `session.events` to extract web source details (URLs,
    titles, domains from `grounding_chunks`) and associated text segments with confidence scores
    (from `grounding_supports`). The aggregated source information and a mapping of URLs to short
    IDs are cumulatively stored in `callback_context.state`.

    Args:
        callback_context (CallbackContext): The context object providing access to the agent's
            session events and persistent state.
    """
    session = callback_context._invocation_context.session
    url_to_short_id = callback_context.state.get("url_to_short_id", {})
    sources = callback_context.state.get("sources", {})
    id_counter = len(url_to_short_id) + 1
    for event in session.events:
        if not (event.grounding_metadata and event.grounding_metadata.grounding_chunks):
            continue
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
                        sources[short_id]["supported_claims"].append(
                            {
                                "text_segment": text_segment,
                                "confidence": confidence,
                            }
                        )
    callback_context.state["url_to_short_id"] = url_to_short_id
    callback_context.state["sources"] = sources


def citation_replacement_callback(
    callback_context: CallbackContext,
) -> types.Content | None:
    """Replaces citation tags in a report with Markdown-formatted links.

    Processes 'combined_final_cited_report' from context state, converting tags like
    `<cite source="src-N"/>` into hyperlinks using source information from
    `callback_context.state["sources"]`. Also fixes spacing around punctuation.

    Args:
        callback_context (CallbackContext): Contains the report and source information.

    Returns:
        types.Content: The processed report with Markdown citation links.
    """
    # types.Content: The processed report with Markdown citation links.
    final_report = callback_context.state.get("combined_final_cited_report", "")
    sources = callback_context.state.get("sources", {})
    callback_context.state["final_report_with_citations"] = render_citations(
        final_report, sources
    )
    return types.Content(parts=[types.Part(text="Research report composed 📝")])
