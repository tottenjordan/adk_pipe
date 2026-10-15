"""Ad copy stage of the creative pipeline: drafter, critic and the
deterministic copy gate with its bounded, targeted reviser loop, composed into
``ad_creative_pipeline``. The gate reads the run's ``RunPolicy`` once per
decision (``creative_agent.run_policy``).
"""

import copy
import logging
from collections.abc import Mapping
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.planners import BuiltInPlanner
from google.adk.workflow import Workflow
from google.genai import types

from agent_common import FailSoftNode, PipelineRequest, build_gemini, is_populated
from agent_common.sampling import temperature as sampled_temperature

from . import callbacks, prompts
from .brief_check import parse_brief
from .config import SCHEMA_RETRY, config
from .copy_gate import (
    CopyIssue,
    enforce_timely_copy_cap,
    flatten_copy_issues,
    format_copy_issues,
    gate_copies,
    residual_issues,
    safer_angle_named_copies,
    structural_issues,
)
from .research_stage import item_count, missing_notice
from .run_policy import RunPolicy
from .schemas import AdCopyList, FinalAdCopyList

# Every LlmAgent below sets mode="single_turn" explicitly (see the note in
# research_stage.py): graph nodes would otherwise default to "chat" mode and
# stall the graph on an empty model turn.


# --- AD COPY AGENT (DRAFT) ---
ad_copy_drafter = Agent(
    model=build_gemini(config.worker_model),
    name="ad_copy_drafter",
    mode="single_turn",
    include_contents="none",
    description="Generate 10 initial ad copy ideas based on campaign guidelines and trends",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.AD_COPY_DRAFTER_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=sampled_temperature(1.5),
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "ad_copy_drafter",
        },
    ),
    output_schema=AdCopyList,
    retry_config=SCHEMA_RETRY,
    output_key="ad_copy_draft",
    before_agent_callback=callbacks.reset_copy_state,
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=[
        callbacks.scrub_surrogates_in_response,
        callbacks.log_empty_turn_finish_reason,
    ],
)


# --- AD COPY CRITIC AGENT ---
ad_copy_critic = Agent(
    # Lever C: critique/narrow-down of already-generated ad copies is a low-
    # quality-dependence step, so run it on worker_model (flash) instead of
    # critic_model (pro) to drop one serial 5-RPM PRO turn from the ad_copy phase.
    model=build_gemini(config.worker_model),
    name="ad_copy_critic",
    mode="single_turn",
    include_contents="none",
    description="Critique and narrow down ad copies based on product, audience, and trends",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.AD_COPY_CRITIC_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=sampled_temperature(0.7),
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "ad_copy_critic",
        },
    ),
    output_schema=FinalAdCopyList,
    retry_config=SCHEMA_RETRY,
    output_key="ad_copy_critique",
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=[
        callbacks.scrub_surrogates_in_response,
        callbacks.log_empty_turn_finish_reason,
    ],
)


# --- AD COPY REVISER (bounded, targeted revision) --- #
# Rewrites ONLY the final copies copy_gate flagged, fixing exactly their listed
# issues (`{ad_copy_issues?}`), and returns all copies under the same
# output_key. Same model/schema/retry/callbacks as ad_copy_critic, plus the
# safety net: restore_unflagged_copies_callback reverts any copy the gate did
# not flag (and restores dropped/duplicated ids) from the gate's pre-revision
# snapshot. Exported bare via the facade for interactive checkpoint-2 reuse.
#
# Its prompt also reads `{ad_copy_feedback?}` (user feedback). The copy_gate
# loop neither sets nor relies on it: in creative_agent it is always unset, and
# reset_copy_state deliberately leaves it alone (it is user input, not gate
# state). interactive_creative's checkpoint-2 revision sets it (with every copy
# flagged) via prepare_copy_revision and keeps it afterwards for the visual
# steps: ad_creative_pipeline (whose gate would replay it) does not run again
# after checkpoint 2.
ad_copy_reviser = Agent(
    model=build_gemini(config.worker_model),
    name="ad_copy_reviser",
    mode="single_turn",
    include_contents="none",
    description="Revises only the flagged final ad copies to fix the issues found by the copy gate",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.AD_COPY_REVISER_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=sampled_temperature(0.7),
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "ad_copy_reviser",
        },
    ),
    output_schema=FinalAdCopyList,
    retry_config=SCHEMA_RETRY,
    output_key="ad_copy_critique",
    after_agent_callback=callbacks.restore_unflagged_copies_callback,
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=[
        callbacks.scrub_surrogates_in_response,
        callbacks.log_empty_turn_finish_reason,
    ],
)


# --- DETERMINISTIC COPY GATE (bounded revision loop) --- #
# copy_gate runs creative_agent.copy_gate on the critic's final copies:
# deterministic checks (product named, CTA <= 8 words (6 under the weak_cta
# rating strictness), headline/caption length, the brief's avoid terms,
# unsupported absolute claims, with a timely moment dated/outcome terms in
# evergreen copies, and a trend tie: each copy names the trend, one of its
# distinctive words or a brief motif, unless the current moment is sensitive)
# plus the critic's self-reported failed
# `proposition`/`mandatories`/`risks`/`trend_bridge` brief checks (and
# `reason_to_believe` under the off_brief rating strictness; its other failed
# items are advisory and never gate). Same contract as brief_gate: with issues and revision budget
# left (config.copy_revision_rounds, env COPY_REVISION_ROUNDS, 0-2) it writes
# them as a Markdown list grouped per copy (`ad_copy_issues`, the reviser's
# input), the flagged ids and a pre-revision snapshot (the reviser's safety
# net), bumps the counter and routes "revise"; the reviser routes back to the
# gate, which re-checks. When the budget is spent, only the DETERMINISTIC
# issues left are recorded as `ad_copy_critique__issues` (surfaced by
# collect_degradation_warnings) — a critic's self-assessment never becomes a
# user-visible warning — and it routes "ok". Every "ok" exit clears the
# revision inputs and also records copy_gate.structural_issues (fewer than 4
# copies; with a brief, copies missing a gating brief_checks item, as one
# line): warning-only, never a revision, because the per-copy reviser cannot
# add a missing copy. The brand name alone does not name the product (as in the
# judge's product_named gate). Avoid
# terms contained in the product name, a non-negative brief mandatory or a
# Title-Case name in the trend (target_search_trends) are exempt.
_COPY_REVISION_CLEARED: dict[str, Any] = {
    "ad_copy_issues": "",
    "ad_copy_flagged_ids": None,
    "ad_copy_critique__before_revision": None,
}


def _copy_issues(
    state: Mapping[str, Any], policy: RunPolicy | None = None
) -> dict[str, list[CopyIssue]]:
    return gate_copies(
        state.get("ad_copy_critique"), policy or RunPolicy.from_state(state, config)
    )


def _safer_angle_delta(policy: RunPolicy, critique: Any) -> dict[str, Any]:
    """``safer_angle_named_copies`` (the copies naming the trend's subject)
    when the safer angle is active; {} otherwise, so other runs' deltas are
    unchanged. The brief's raw avoid list (not the gate's filtered terms)."""
    if not policy.safer_angle_active:
        return {}
    return {
        "safer_angle_named_copies": safer_angle_named_copies(
            critique,
            trend_terms=policy.trend,
            avoid=policy.avoid,
            risks=policy.risks,
            safer_angle=policy.safer_angle_text,
        )
    }


def _residual(
    state: Mapping[str, Any], critique: Any, issues: dict[str, list[CopyIssue]]
) -> list[str] | None:
    """The deterministic issues as residual-issue strings, plus the list-level
    ``structural_issues`` (warning-only: they never route a revision, since the
    per-copy reviser cannot add a missing copy; the checklist-gap note only
    when a brief exists). None when there are none."""
    residual = flatten_copy_issues(critique, residual_issues(issues))
    has_brief = parse_brief(state.get("creative_brief")) is not None
    return residual + structural_issues(critique, has_brief=has_brief) or None


def _apply_timely_cap(
    state: Mapping[str, Any], policy: RunPolicy | None = None
) -> dict[str, Any]:
    """``{"ad_copy_critique": capped}`` when ``enforce_timely_copy_cap``
    changed the copies (flags beyond MAX_TIMELY_CONCEPTS, or any flag without
    a timely moment, cleared; one warning logged per cleared copy), else {}."""
    critique = state.get("ad_copy_critique")
    if not is_populated(critique):
        return {}
    policy = policy or RunPolicy.from_state(state, config)
    capped, warns = enforce_timely_copy_cap(
        critique, timely_ok=policy.timely_ok, max_timely=policy.max_timely
    )
    for warning in warns:
        logging.warning(f"timely copy cap: {warning}")
    return {} if capped is critique else {"ad_copy_critique": capped}


def residual_copy_issues(state: Mapping[str, Any]) -> list[str] | None:
    """The copy gate's warning-only residual for the current copies (pure).

    The deterministic issues plus the list-level structural issues, exactly
    what the gate records as ``ad_copy_critique__issues`` on an "ok" exit;
    None when clean. Exported via the facade so interactive_creative can
    re-record it after the checkpoint-2 user revision (no revision loop).
    """
    critique = state.get("ad_copy_critique")
    issues = _copy_issues(state) if is_populated(critique) else {}
    return _residual(state, critique, issues)


def cap_and_residual_copies(state: Mapping[str, Any]) -> dict[str, Any]:
    """The timely copy cap plus the residual, as a state delta (pure).

    What the copy gate's "ok" exit records, without a revision loop: first
    ``enforce_timely_copy_cap`` (flags beyond MAX_TIMELY_CONCEPTS, or any flag
    without a timely moment, cleared; the capped ``ad_copy_critique`` is in
    the delta only when it changed), then ``residual_copy_issues`` on the
    capped copies as ``ad_copy_critique__issues``. Exported via the facade
    for interactive_creative's checkpoint-2 user revision.
    """
    policy = RunPolicy.from_state(state, config)
    delta = _apply_timely_cap(state, policy)
    state = {**state, **delta}
    delta["ad_copy_critique__issues"] = residual_copy_issues(state)
    delta.update(_safer_angle_delta(policy, state.get("ad_copy_critique")))
    return delta


def copy_gate_decision(
    state: Mapping[str, Any], max_rounds: int
) -> tuple[str, dict[str, Any]]:
    """The copy gate's (route, state_delta) for a state snapshot (pure).

    The timely cap runs first (``enforce_timely_copy_cap``): flags beyond
    MAX_TIMELY_CONCEPTS, or any flag without a timely moment, are cleared and
    the capped copies are written back (``base``) before they are checked.
    """
    policy = RunPolicy.from_state(state, config)
    base = _apply_timely_cap(state, policy)
    state = {**state, **base}
    critique = state.get("ad_copy_critique")
    issues = _copy_issues(state, policy) if is_populated(critique) else {}
    base.update(_safer_angle_delta(policy, critique))
    if not issues:
        return "ok", {
            **base,
            **_COPY_REVISION_CLEARED,
            "ad_copy_critique__issues": _residual(state, critique, issues),
        }
    used = int(state.get("ad_copy_revision_rounds_used") or 0)
    if used < max_rounds:
        return "revise", {
            **base,
            "ad_copy_issues": format_copy_issues(critique, issues),
            "ad_copy_flagged_ids": list(issues),
            "ad_copy_critique__before_revision": copy.deepcopy(critique),
            "ad_copy_revision_rounds_used": used + 1,
        }
    return "ok", {
        **base,
        **_COPY_REVISION_CLEARED,
        "ad_copy_critique__issues": _residual(state, critique, issues),
    }


def copy_gate(ctx: Context) -> Event:
    """Route flagged copies to the reviser while budget remains."""
    route, delta = copy_gate_decision(ctx.state.to_dict(), config.copy_revision_rounds)
    if delta.get("ad_copy_critique__issues"):
        logging.warning("ad copy issues remain: %s", delta["ad_copy_critique__issues"])
    return Event(actions=EventActions(route=route, state_delta=delta))


# A raising reviser (e.g. SCHEMA_RETRY exhausted) must not fail the ad copy
# step: the critic's copies are already in state. Keep the pre-revision copies,
# record the gate's issues as ad_copy_critique__issues and spend the budget so
# the gate (which the failsoft node routes back to) does not re-run it.
def _ad_copy_reviser_failed(state: Mapping[str, Any], exc: Exception) -> dict[str, Any]:
    before = state.get("ad_copy_critique__before_revision")
    critique = before if is_populated(before) else state.get("ad_copy_critique")
    used = int(state.get("ad_copy_revision_rounds_used") or 0)
    policy = RunPolicy.from_state(state, config)
    issues = _copy_issues({**state, "ad_copy_critique": critique}, policy)
    return {
        **_COPY_REVISION_CLEARED,
        "ad_copy_critique": critique,
        "ad_copy_critique__issues": _residual(state, critique, issues),
        "ad_copy_revision_rounds_used": max(used, config.copy_revision_rounds),
        **_safer_angle_delta(policy, critique),
    }


ad_copy_reviser_failsoft = FailSoftNode(
    name="ad_copy_reviser_failsoft",
    node=ad_copy_reviser,
    on_error=_ad_copy_reviser_failed,
)


def ad_copies_ready(ctx: Context) -> Any:
    """Terminal node of ad_creative_pipeline (the root's tool result).

    A short confirmation, not the copies themselves (like research_report_ready):
    every later step reads `ad_copy_critique` from state, and echoing ~10k chars
    of copy JSON into the root's context made the Pro root prone to empty turns.
    A non-empty notice when the critic produced none.
    """
    value = ctx.state.get("ad_copy_critique")
    if is_populated(value):
        return (
            f"Ad copies complete: {item_count(value, 'ad_copies')} final copies "
            "saved to session state as 'ad_copy_critique'."
        )
    return missing_notice("ad_copy_critic", "ad_copy_critique")


# Ad creative generation graph (draft → critique → deterministic copy gate with
# a bounded, targeted revision loop), ending in a truthy result node.
ad_creative_pipeline = Workflow(
    name="ad_creative_pipeline",
    description="Generates ad copy drafts with an actor-critic workflow, then revises copies that fail the brief checks.",
    input_schema=PipelineRequest,
    edges=[
        ("START", ad_copy_drafter, ad_copy_critic, copy_gate),
        (copy_gate, {"ok": ad_copies_ready, "revise": ad_copy_reviser_failsoft}),
        (ad_copy_reviser_failsoft, copy_gate),
    ],
)
