import copy
import logging
from collections.abc import Mapping
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.planners import BuiltInPlanner
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from google.adk.workflow import JoinNode, Workflow
from google.genai import types

from agent_common import (
    ROOT_EMPTY_TURN_RETRIES,
    FailSoftNode,
    PipelineRequest,
    RetryUntilKeyNode,
    build_gemini,
    build_gemini_with_fallback,
    build_safety_plugins,
    is_populated,
)
from creative_eval.agent import creative_eval_agent

from . import callbacks, prompts, tools
from .brief_check import check_brief
from .brief_render import render_brief_markdown
from .config import INFRA_RETRY, SCHEMA_RETRY, config
from .copy_gate import (
    CopyIssue,
    brief_avoid,
    flatten_copy_issues,
    format_copy_issues,
    gate_copies,
    residual_issues,
)
from .schemas import (  # noqa: F401
    AdCopy,
    AdCopyList,
    BrandCues,
    BriefCheck,
    CreativeAngle,
    CreativeBrief,
    FinalAdCopy,
    FinalAdCopyList,
    ReasonToBelieve,
    ResearchFeedback,
    SearchQuery,
    TrendBridge,
    VisualConcept,
    VisualConceptCritique,
    VisualConceptCritiqueList,
    VisualConceptFinal,
    VisualConceptFinalList,
    VisualConceptList,
)
from .sub_agents.campaign_researcher.agent import ca_sequential_planner
from .sub_agents.trend_researcher.agent import gs_sequential_planner

# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


# Every LlmAgent placed in a graph Workflow below sets mode="single_turn"
# explicitly: an agent that gets a parent_agent (as graph nodes do) otherwise
# defaults to "chat" mode (wait_for_output=True), which would stall the graph on
# an empty model turn instead of moving on. single_turn injects the predecessor node's
# output as a user turn; every instruction here reads its inputs only through
# `{state}` tokens (and all of them keep include_contents="none"), so that
# injected turn is not load-bearing.


# --- RESEARCH MERGE --- #
merge_planners = Agent(
    name="merge_planners",
    model=build_gemini(config.worker_model),
    mode="single_turn",
    include_contents="none",
    description="Combine results from state keys 'campaign_web_search_insights' and 'gs_web_search_insights'",
    instruction=prompts.MERGE_PLANNERS_INSTR,
    output_key="combined_web_search_insights",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


combined_web_evaluator = Agent(
    model=build_gemini_with_fallback(config.critic_model, config.critic_fallback_model),
    name="combined_web_evaluator",
    mode="single_turn",
    include_contents="none",
    description="Critically evaluates research about the campaign guide and generates follow-up queries.",
    instruction=prompts.COMBINED_WEB_EVALUATOR_INSTR,
    output_schema=ResearchFeedback,
    retry_config=SCHEMA_RETRY,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    output_key="combined_research_evaluation",
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# WS2 split — searcher half: runs the follow-up google_search and emits RAW
# findings only. Separating tool-use from synthesis is the durable fix for the
# empty-turn flake. Grounding metadata lives on this turn, so
# `collect_research_sources_callback` stays here.
enhanced_combined_searcher = Agent(
    model=build_gemini(config.worker_model),
    name="enhanced_combined_searcher",
    mode="single_turn",
    include_contents="none",
    description="Executes follow-up searches and returns raw new findings.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.ENHANCED_COMBINED_SEARCHER_INSTR,
    tools=[google_search],
    output_key="refined_web_search_raw",
    after_agent_callback=callbacks.collect_research_sources_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# WS2 split — synthesizer half: tool-free / planner-free. Reads the raw follow-up
# findings (optional `{...?}` so an empty searcher turn degrades to empty synthesis
# and the wrapper retries the whole pair rather than raising KeyError inside it) and
# shapes them into the existing "New Research Findings" summary.
refined_web_synthesizer = Agent(
    model=build_gemini(config.worker_model),
    name="refined_web_synthesizer",
    mode="single_turn",
    include_contents="none",
    description="Synthesizes the raw follow-up findings into a concise new-insights summary.",
    instruction=prompts.REFINED_WEB_SYNTHESIZER_INSTR,
    output_key="refined_web_search_insights",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)

refined_search_and_synthesize = Workflow(
    name="refined_search_and_synthesize",
    description="Runs the follow-up web search then synthesizes the new findings.",
    edges=[("START", enhanced_combined_searcher, refined_web_synthesizer)],
)


# Retry-on-empty: if the searcher OR synthesizer emits no final text (leaving
# `refined_web_search_insights` unset), re-run the whole pair until populated
# (bounded). combined_report_composer already guards with
# `{refined_web_search_insights?}`, but retrying recovers the refinement (a
# quality gain) instead of silently dropping it. Each attempt re-runs the whole
# Workflow pair (under a distinct run_id).
enhanced_combined_searcher_resilient = RetryUntilKeyNode(
    name="enhanced_combined_searcher_resilient",
    node=refined_search_and_synthesize,
    output_key="refined_web_search_insights",
    max_attempts=3,
)


# `{refined_web_search_insights?}` is intentionally OPTIONAL (trailing `?`): the upstream
# enhanced_combined_searcher occasionally emits no final text (google_search + thinking
# returning only tool calls), leaving its output_key unset. Without the `?`, ADK raises
# `KeyError: Context variable not found` here and aborts the whole run after the expensive
# research. The refinement is additive — the full base research is in
# `{combined_web_search_insights}` — so degrading to an empty section is the right fallback.
combined_report_composer = Agent(
    model=build_gemini_with_fallback(config.critic_model, config.critic_fallback_model),
    name="combined_report_composer",
    mode="single_turn",
    include_contents="none",
    description="Transforms research data and a markdown outline into a final, cited report.",
    instruction=prompts.COMBINED_REPORT_COMPOSER_INSTR,
    output_key="combined_final_cited_report",
    after_agent_callback=callbacks.citation_replacement_callback,
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# --- STRUCTURED CREATIVE BRIEF --- #
# After the cited report, a worker-bucket agent distils it (plus the campaign
# inputs) into a structured, fit-tested CreativeBrief — the contract the ad copy
# and visual agents deliver against. Writer and reviser come from one factory so
# their model/schema/retry/callbacks cannot drift; they share one instruction,
# whose revision block only applies when `{brief_issues?}` is non-empty (the
# deterministic brief_gate fills it before routing to the reviser). Only the
# writer resets the per-run brief state (see callbacks.reset_brief_state).
#
# When the composer produced no report, the writer still runs: the instruction
# tells it to build the brief from the campaign inputs alone (RTBs cite
# "brief", conservative fit score), which keeps the creative stages on a
# structured contract instead of skipping it.
def _build_brief_agent(
    name: str, description: str, *, reset_state: bool = False
) -> Agent:
    return Agent(
        model=build_gemini(config.worker_model),
        name=name,
        mode="single_turn",
        include_contents="none",
        description=description,
        planner=BuiltInPlanner(
            thinking_config=types.ThinkingConfig(include_thoughts=False)
        ),
        instruction=prompts.CREATIVE_BRIEF_WRITER_INSTR,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.7,
            labels={
                "agentic_wf": "trend_scout",
                "agent": "creative_agent",
                "subagent": name,
            },
        ),
        output_schema=CreativeBrief,
        retry_config=SCHEMA_RETRY,
        output_key="creative_brief",
        before_agent_callback=callbacks.reset_brief_state if reset_state else None,
        before_model_callback=callbacks.rate_limit_callback,
        after_model_callback=[
            callbacks.scrub_surrogates_in_response,
            callbacks.log_empty_turn_finish_reason,
        ],
    )


brief_writer = _build_brief_agent(
    "brief_writer",
    "Distils the research report into a structured, fit-tested creative brief.",
    reset_state=True,
)
brief_reviser = _build_brief_agent(
    "brief_reviser",
    "Revises the creative brief to fix the issues found by the brief check.",
)

# Retry-on-empty: a structured turn that comes back empty leaves creative_brief
# unset; one fresh attempt usually recovers it. On exhaustion the marker
# creative_brief__retry_exhausted is surfaced by collect_degradation_warnings and
# the creative agents fall back to the research report (`{creative_brief_md?}`
# stays empty).
brief_writer_resilient = RetryUntilKeyNode(
    name="brief_writer_resilient",
    node=brief_writer,
    output_key="creative_brief",
    max_attempts=2,
)


# --- FAIL-SOFT BRIEF STEPS --- #
# The brief is an enrichment on top of the already-written research report, so
# an EXCEPTION in the writer or reviser (e.g. SCHEMA_RETRY exhausted on the
# large CreativeBrief schema — RetryUntilKeyNode only retries empty output) must
# not fail the research step. FailSoftNode logs it and applies these deltas:
# - writer: no brief + creative_brief__retry_exhausted (same degradation as an
#   empty-output exhaustion; the creatives fall back to the report);
# - reviser: keep the pre-revision brief, record the gate's issues as
#   creative_brief__issues, and spend the revision budget (a raising reviser is
#   not re-run by the gate).
def _brief_writer_failed(state: Mapping[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "creative_brief": None,
        "creative_brief_md": "",
        "brief_issues": "",
        "creative_brief__retry_exhausted": True,
    }


def _brief_reviser_failed(state: Mapping[str, Any], exc: Exception) -> dict[str, Any]:
    used = int(state.get("brief_revision_rounds_used") or 0)
    return {
        "creative_brief": state.get("creative_brief"),
        "creative_brief_md": render_brief_markdown(
            state.get("creative_brief"), heading=False
        ),
        "brief_issues": "",
        "creative_brief__issues": _brief_issues(state) or None,
        "brief_revision_rounds_used": max(used, config.brief_revision_rounds),
    }


brief_writer_failsoft = FailSoftNode(
    name="brief_writer_failsoft",
    node=brief_writer_resilient,
    on_error=_brief_writer_failed,
)
brief_reviser_failsoft = FailSoftNode(
    name="brief_reviser_failsoft",
    node=brief_reviser,
    on_error=_brief_reviser_failed,
)


# --- DETERMINISTIC BRIEF GATE (bounded revision loop) --- #
# brief_gate runs creative_agent.brief_check on the current brief. With issues
# and revision budget left (config.brief_revision_rounds, env
# BRIEF_REVISION_ROUNDS, 0-2) it writes them as a bulleted `brief_issues`
# string — the reviser's `{brief_issues?}` revision input — bumps the counter
# and routes "revise"; the reviser routes back to the gate, which re-checks the
# revised brief. So BRIEF_REVISION_ROUNDS is exactly the maximum number of
# reviser passes (the counter bounds the routed cycle). When the budget is spent
# with issues left, the gate records them as `creative_brief__issues` (surfaced
# by collect_degradation_warnings) and routes "ok". `brief_issues` is cleared on
# every "ok" exit so it is only non-empty while the reviser runs, and every exit
# writes `creative_brief_md` — the compact Markdown the creative agents read.
#
# A missing brief (writer exhausted its retries or failed) routes "ok" without a
# revision: there is nothing to revise, and creative_brief__retry_exhausted
# already reports it; the creative agents then fall back to the report.
def _brief_issues(state: Mapping[str, Any]) -> list[str]:
    sources = state.get("sources")
    return check_brief(
        state.get("creative_brief"),
        brand_colors=str(state.get("brand_colors") or ""),
        brand=str(state.get("brand") or ""),
        target_product=str(state.get("target_product") or ""),
        sources=sources if isinstance(sources, Mapping) else None,
    )


def brief_gate_decision(
    state: Mapping[str, Any], max_rounds: int
) -> tuple[str, dict[str, Any]]:
    """The brief gate's (route, state_delta) for a state snapshot (pure)."""
    brief = state.get("creative_brief")
    if not is_populated(brief):
        return "ok", {"brief_issues": "", "creative_brief_md": ""}
    brief_md = render_brief_markdown(brief, heading=False)
    issues = _brief_issues(state)
    if not issues:
        return "ok", {
            "brief_issues": "",
            "creative_brief__issues": None,
            "creative_brief_md": brief_md,
        }
    used = int(state.get("brief_revision_rounds_used") or 0)
    if used < max_rounds:
        return "revise", {
            "brief_issues": "\n".join(f"- {issue}" for issue in issues),
            "brief_revision_rounds_used": used + 1,
            "creative_brief_md": brief_md,
        }
    return "ok", {
        "brief_issues": "",
        "creative_brief__issues": issues,
        "creative_brief_md": brief_md,
    }


def brief_gate(ctx: Context) -> Event:
    """Route the brief to the reviser while it fails the check and budget remains."""
    route, delta = brief_gate_decision(
        ctx.state.to_dict(), config.brief_revision_rounds
    )
    if delta.get("creative_brief__issues"):
        logging.warning(
            "creative brief issues remain: %s", delta["creative_brief__issues"]
        )
    return Event(actions=EventActions(route=route, state_delta=delta))


# --- CONDITIONAL RESEARCH REFINEMENT GATE (Lever A) --- #
# The evaluator (gemini-3.1-pro-preview) + follow-up searcher form a SECOND,
# additive research round: the base brief in `combined_web_search_insights`
# (two parallel deep researchers → synthesis) already flows straight to
# `combined_report_composer`, which guards the refined input with the optional
# `{refined_web_search_insights?}`. So the refinement is only *worth* an extra
# serial PRO call when the base research came back thin.
#
# `_base_research_is_degraded` gates the round on exactly that: run it only when
# the merged brief is blank/missing, or an upstream producer exhausted its
# retries (`*__retry_exhausted`, set by the RetryUntilKeyNode wrappers on the
# gs/campaign producers). On the healthy common path `refinement_gate` routes
# straight to the composer — dropping one gemini-3.1-pro-preview call (the 5 RPM
# quota is the wall-clock bottleneck) plus a google_search + synthesis pass —
# while keeping the round as a self-healing fallback for degraded runs. No
# `output_key`/`{var?}` guard is disturbed: the evaluator's output is consumed
# only inside the round, and the composer already tolerates a missing
# `refined_web_search_insights`.
def _base_research_is_degraded(state: Mapping[str, Any]) -> bool:
    """True when the base research is thin enough to warrant a refinement round."""
    brief = state.get("combined_web_search_insights")
    if not (isinstance(brief, str) and brief.strip()):
        return True
    for marker in (
        "gs_web_search_insights__retry_exhausted",
        "campaign_web_search_insights__retry_exhausted",
    ):
        if state.get(marker):
            return True
    return False


def refinement_gate_route(state: Mapping[str, Any]) -> str:
    """The refinement gate's route: ``"refine"`` when degraded, else ``"skip"``."""
    return "refine" if _base_research_is_degraded(state) else "skip"


# Barrier: waits for BOTH research branches before merging.
research_join = JoinNode(name="research_join")


def research_barrier() -> None:
    """No-output pass-through between the join and merge_planners.

    A JoinNode's output is a dict keyed by upstream node name; a single_turn
    merge_planners would receive it as its injected user turn. Yielding nothing
    here keeps that dict out of its prompt — merge_planners reads both research
    reports from state (`{campaign_web_search_insights?}` /
    `{gs_web_search_insights?}`).
    """
    return None


def refinement_gate(ctx: Context) -> Event:
    """Route to the refinement round only when the base research is degraded."""
    # EventActions(route=...) is the typed spelling of Event(route=...). An ADK
    # State is not a Mapping; to_dict() snapshots it (committed + pending delta).
    route = refinement_gate_route(ctx.state.to_dict())
    return Event(actions=EventActions(route=route))


# --- PIPELINE RESULT NODES --- #
# A pipeline exposed to a root agent as a tool (auto-wrapped into a NodeTool)
# MUST finish with a truthy output: a Workflow ending with no output, or a falsy
# one, silently stalls the root's turn (no function response; see
# tests/test_workflow_api_contract.py). A final LlmAgent can emit an empty turn
# (the flake the retry wrappers exist for), so each LlmAgent-terminated pipeline
# ends in one of these function nodes instead: it returns a truthy result when
# the pipeline's output key is populated, else a short non-empty notice (the key
# itself stays unset so downstream `{var?}` guards still apply). "Populated" is
# the same check RetryUntilKeyNode uses (agent_common.retry_node.is_populated).


def _missing_notice(producer: str, key: str) -> str:
    return (
        f"{producer} did not produce '{key}'; it is unavailable for this run. "
        "Continue with the next workflow step."
    )


def research_report_ready(ctx: Context) -> str:
    """Terminal node of combined_research_pipeline (the root's tool result).

    A short confirmation, not the report itself, mirroring the pre-graph
    AgentTool result (the composer's citation-callback text): the report lives
    in state for save_draft_report_artifact and the creative stages, and
    repeating it in the root's context would only add tokens.
    """
    brief_note = (
        " Structured creative brief saved as 'creative_brief'."
        if is_populated(ctx.state.get("creative_brief"))
        else " No structured creative brief is available for this run."
    )
    if is_populated(ctx.state.get("combined_final_cited_report")):
        return (
            "Research report complete: saved to session state as "
            "'combined_final_cited_report' (with resolved citations in "
            "'final_report_with_citations')." + brief_note
        )
    return (
        _missing_notice("combined_report_composer", "combined_final_cited_report")
        + brief_note
    )


# --- COMPLETE RESEARCH PIPELINE --- #
# Graph: both research chains fan out from START and run concurrently, a
# JoinNode waits for both, the barrier drops the join dict, merge_planners
# synthesizes the base brief, and the gate routes either through the refinement
# round (degraded research) or straight to the composer (healthy path). The
# composer's report is then distilled into the structured creative brief, which
# brief_gate either accepts or sends through a bounded revision loop.
combined_research_pipeline = Workflow(
    name="combined_research_pipeline",
    description="Runs parallel campaign + trend research, a refinement round only when that research is degraded, then a cited report and a structured creative brief.",
    input_schema=PipelineRequest,
    edges=[
        (
            "START",
            (gs_sequential_planner, ca_sequential_planner),
            research_join,
            research_barrier,
            merge_planners,
            refinement_gate,
        ),
        (
            refinement_gate,
            {"refine": combined_web_evaluator, "skip": combined_report_composer},
        ),
        (
            combined_web_evaluator,
            enhanced_combined_searcher_resilient,
            combined_report_composer,
            brief_writer_failsoft,
            brief_gate,
        ),
        (brief_gate, {"ok": research_report_ready, "revise": brief_reviser_failsoft}),
        # The routed cycle: the gate's counter bounds the reviser passes.
        (brief_reviser_failsoft, brief_gate),
    ],
)


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
        temperature=1.5,
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
        temperature=0.7,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "ad_copy_critic",
        },
    ),
    output_schema=FinalAdCopyList,
    retry_config=SCHEMA_RETRY,
    output_key="ad_copy_critique",
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
        temperature=0.7,
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
    after_model_callback=[
        callbacks.scrub_surrogates_in_response,
        callbacks.log_empty_turn_finish_reason,
    ],
)


# --- DETERMINISTIC COPY GATE (bounded revision loop) --- #
# copy_gate runs creative_agent.copy_gate on the critic's final copies:
# deterministic checks (product named, CTA <= 8 words, headline/caption length,
# the brief's avoid terms) plus the critic's self-reported failed
# `proposition`/`mandatories` brief checks (its other failed items are advisory
# and never gate). Same contract as brief_gate: with issues and revision budget
# left (config.copy_revision_rounds, env COPY_REVISION_ROUNDS, 0-2) it writes
# them as a Markdown list grouped per copy (`ad_copy_issues`, the reviser's
# input), the flagged ids and a pre-revision snapshot (the reviser's safety
# net), bumps the counter and routes "revise"; the reviser routes back to the
# gate, which re-checks. When the budget is spent, only the DETERMINISTIC
# issues left are recorded as `ad_copy_critique__issues` (surfaced by
# collect_degradation_warnings) — a critic's self-assessment never becomes a
# user-visible warning — and it routes "ok". Every "ok" exit clears the
# revision inputs.
_COPY_REVISION_CLEARED: dict[str, Any] = {
    "ad_copy_issues": "",
    "ad_copy_flagged_ids": None,
    "ad_copy_critique__before_revision": None,
}


def _copy_issues(state: Mapping[str, Any]) -> dict[str, list[CopyIssue]]:
    return gate_copies(
        state.get("ad_copy_critique"),
        target_product=str(state.get("target_product") or ""),
        avoid=brief_avoid(state.get("creative_brief")),
    )


def _residual(critique: Any, issues: dict[str, list[CopyIssue]]) -> list[str] | None:
    """The deterministic issues as residual-issue strings (None when none)."""
    return flatten_copy_issues(critique, residual_issues(issues)) or None


def copy_gate_decision(
    state: Mapping[str, Any], max_rounds: int
) -> tuple[str, dict[str, Any]]:
    """The copy gate's (route, state_delta) for a state snapshot (pure)."""
    critique = state.get("ad_copy_critique")
    issues = _copy_issues(state) if is_populated(critique) else {}
    if not issues:
        return "ok", {**_COPY_REVISION_CLEARED, "ad_copy_critique__issues": None}
    used = int(state.get("ad_copy_revision_rounds_used") or 0)
    if used < max_rounds:
        return "revise", {
            "ad_copy_issues": format_copy_issues(critique, issues),
            "ad_copy_flagged_ids": list(issues),
            "ad_copy_critique__before_revision": copy.deepcopy(critique),
            "ad_copy_revision_rounds_used": used + 1,
        }
    return "ok", {
        **_COPY_REVISION_CLEARED,
        "ad_copy_critique__issues": _residual(critique, issues),
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
    issues = _copy_issues({**state, "ad_copy_critique": critique})
    return {
        **_COPY_REVISION_CLEARED,
        "ad_copy_critique": critique,
        "ad_copy_critique__issues": _residual(critique, issues),
        "ad_copy_revision_rounds_used": max(used, config.copy_revision_rounds),
    }


ad_copy_reviser_failsoft = FailSoftNode(
    name="ad_copy_reviser_failsoft",
    node=ad_copy_reviser,
    on_error=_ad_copy_reviser_failed,
)


def ad_copies_ready(ctx: Context) -> Any:
    """Terminal node of ad_creative_pipeline (the root's tool result).

    Returns the final ad copies (the critic's, after any gate-driven revision;
    the payload the pre-graph AgentTool returned), or a non-empty notice when
    the critic produced none.
    """
    value = ctx.state.get("ad_copy_critique")
    if is_populated(value):
        return value
    return _missing_notice("ad_copy_critic", "ad_copy_critique")


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


# --- ART DIRECTOR AGENT ---
# Sets the campaign-wide visual direction (mood, palette, motifs, brand cues,
# recommended diverse style families) BEFORE individual concepts are drafted, so the
# concepts are cohesive and on-brand. Plain-text output (no output_schema) → the brief
# is prose guidance, consumed by the drafter via {visual_direction}.
art_director = Agent(
    model=build_gemini(config.worker_model),
    name="art_director",
    mode="single_turn",
    include_contents="none",
    description="Set the campaign-wide visual direction before concept drafting",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.ART_DIRECTOR_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.9,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "art_director",
        },
    ),
    output_key="visual_direction",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# --- VISUAL CONCEPT DRAFT AGENT ---
visual_concept_drafter = Agent(
    model=build_gemini(config.worker_model),
    name="visual_concept_drafter",
    mode="single_turn",
    include_contents="none",
    description="Generate initial visual concepts for selected ad copies",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.VISUAL_CONCEPT_DRAFTER_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=1.5,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_concept_drafter",
        },
    ),
    output_schema=VisualConceptList,
    retry_config=SCHEMA_RETRY,
    output_key="visual_draft",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# --- VISUAL CONCEPT CRITIQUE AGENT ---
visual_concept_critic = Agent(
    # Lever C: same rationale as ad_copy_critic — narrowing existing visual
    # concepts is low-quality-dependence, so use worker_model (flash) not pro.
    model=build_gemini(config.worker_model),
    name="visual_concept_critic",
    mode="single_turn",
    include_contents="none",
    description="Critique and refine visual concepts",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.VISUAL_CONCEPT_CRITIC_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.7,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_concept_critic",
        },
    ),
    output_schema=VisualConceptCritiqueList,
    retry_config=SCHEMA_RETRY,
    output_key="visual_concept_critique",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# --- VISUAL CONCEPT FINAL AGENT ---
visual_concept_finalizer = Agent(
    model=build_gemini(config.worker_model),
    name="visual_concept_finalizer",
    mode="single_turn",
    include_contents="none",
    description="Finalize visual concepts to proceed with.",
    instruction=prompts.VISUAL_CONCEPT_FINALIZER_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.8,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_concept_finalizer",
        },
    ),
    output_schema=VisualConceptFinalList,
    retry_config=SCHEMA_RETRY,
    output_key="final_visual_concepts",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
    # Deterministic last-line guard: every final image prompt names the concept's
    # trend_motif and the target product (repairs + warns; runs after the
    # output_key write, so generate_image reads the repaired prompts).
    after_agent_callback=callbacks.ensure_trend_and_product_callback,
)


# --- VISUAL GENERATOR AGENT ---
# Runs generate_image over the finalized visual concepts. In creative_agent it is
# chained into visual_production_pipeline (below) so image rendering is deterministic
# and the orchestrator cannot skip it. interactive_creative deliberately invokes it as
# a separate step AFTER a human review checkpoint (review concepts before spending on
# image generation), so it must also remain usable as a standalone agent.
visual_generator = Agent(
    model=build_gemini_with_fallback(config.critic_model, config.critic_fallback_model),
    name="visual_generator",
    mode="single_turn",
    retry_config=INFRA_RETRY,
    include_contents="none",
    description="Generate final visuals using image generation tools",
    # thinking_level=LOW: this is a mechanical single-tool step, not a reasoning task,
    # so we constrain thinking to keep the model from emitting MULTIPLE parallel
    # `generate_image` calls in one turn — parallel calls all read state before any
    # commits, so the tool's idempotency guard (_images_generated) can't dedupe them,
    # causing every image to be rendered 2x (double the image-gen cost). One call is
    # all that's needed: generate_image loops over every concept in
    # final_visual_concepts. (The real dedup safeguard is _images_generated + the
    # "call EXACTLY ONCE" instruction; the low thinking level is belt-and-suspenders.)
    # NOTE: gemini-3.x deprecated the numeric thinking_budget; thinking_budget=0 also
    # never disabled thinking on gemini-3 (that only worked on 2.5). LOW is the lowest
    # level Pro supports — MINIMAL is Flash/Flash-Lite only and 400s on Pro.
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.LOW, include_thoughts=False
        )
    ),
    instruction=prompts.VISUAL_GENERATOR_INSTR,
    tools=[tools.generate_image],
    generate_content_config=types.GenerateContentConfig(
        temperature=1.2,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_generator",
        },
    ),
    # Chain: rate-limit first, then force the generate_image tool call (issue #116).
    # Both return None, so ADK runs them in order every turn. force_image_tool_call
    # is gated on _images_generated so it only constrains the turn until the image
    # step succeeds — belt (deterministic tool_config) AND suspenders (the resilient
    # retry wrapper below) against the MALFORMED_FUNCTION_CALL empty-gallery flake.
    before_model_callback=[
        callbacks.rate_limit_callback,
        callbacks.force_image_tool_call,
    ],
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# Retry-on-empty for the image step: visual_generator (gemini-3.1-pro-preview)
# intermittently returns MALFORMED_FUNCTION_CALL and never emits the generate_image
# tool call, leaving _images_generated unset and shipping an empty gallery (run
# 2032568396381421568). retry_config=INFRA_RETRY only covers infra EXCEPTIONS, not a
# malformed-call finish reason — so wrap in RetryUntilKeyNode (same pattern as the
# research producers), keyed on the _images_generated flag generate_image already sets
# on success. That flag also makes a re-run safe (idempotency guard → no double image
# spend); on exhaustion the wrapper emits _images_generated__retry_exhausted, which
# collect_degradation_warnings surfaces on the gallery/eval banner. Also exposed
# directly to interactive_creative's root (a bare node → NodeTool), hence the
# PipelineRequest input_schema.
#
# max_attempts=6 (issue #116): the MALFORMED flake is transient — a fresh producer
# turn usually clears it, and each attempt IS an independent turn — but 3 attempts
# occasionally weren't enough, dropping the WHOLE run to zero images. A higher cap
# only costs extra *failed* producer turns (the idempotency guard prevents double
# image spend, and the first successful turn returns immediately), so the common
# path is unchanged while rare-failure recovery odds rise materially.
visual_generator_resilient = RetryUntilKeyNode(
    name="visual_generator_resilient",
    # An explicit description: NodeTool otherwise falls back to "Executes the
    # node: <name>" (AgentTool used to expose an empty one).
    description="Generates the image creatives from the final visual concepts.",
    node=visual_generator,
    output_key="_images_generated",
    max_attempts=6,
    input_schema=PipelineRequest,
)


def visual_concepts_ready(ctx: Context) -> Any:
    """Terminal node of visual_generation_pipeline (the root's tool result).

    Returns the finalized visual concepts (the payload the pre-graph AgentTool
    returned), or a non-empty notice when the finalizer produced none.
    """
    value = ctx.state.get("final_visual_concepts")
    if is_populated(value):
        return value
    return _missing_notice("visual_concept_finalizer", "final_visual_concepts")


def render_barrier() -> None:
    """No-output pass-through between the concepts and the render step.

    visual_generator_resilient validates its input against PipelineRequest (it
    is also a root tool in interactive_creative), so the concepts payload from
    visual_generation_pipeline must not reach it; generate_image reads
    final_visual_concepts from state anyway.
    """
    return None


def images_ready(ctx: Context) -> str:
    """Terminal node of visual_production_pipeline (the root's tool result).

    A short confirmation, mirroring research_report_ready: the rendered image
    artifact keys live in state for save_creative_gallery_html and the eval
    step. When the render step exhausted its retries, a non-empty notice
    (degradation is also surfaced via ``_images_generated__retry_exhausted``).
    """
    if is_populated(ctx.state.get("_images_generated")):
        keys = ctx.state.get("_generated_artifact_keys") or []
        return (
            f"Image creatives rendered: {len(keys)} image artifact(s) saved "
            "(keys in session state '_generated_artifact_keys')."
        )
    return _missing_notice("visual_generator", "_images_generated")


# Graph for visual concepts (draft -> critique -> finalize). Shared with
# interactive_creative, which pauses for human review after this stage before rendering.
visual_generation_pipeline = Workflow(
    name="visual_generation_pipeline",
    description="Generates visual concepts with an actor-critic workflow.",
    input_schema=PipelineRequest,
    edges=[
        (
            "START",
            art_director,
            visual_concept_drafter,
            visual_concept_critic,
            visual_concept_finalizer,
            visual_concepts_ready,
        )
    ],
)


# creative_agent (non-interactive) renders images immediately after finalizing
# concepts, as one deterministic unit. This removes the orchestrator's opportunity to
# skip image generation — which it did when creative_eval_agent looked like the next
# step, jumping straight from visual concepts to evaluation. interactive_creative does
# NOT use this: it keeps concepts and images split around a review checkpoint.
# Ends in images_ready, a short confirmation for the root (the retry node's own
# output would be the bare `_images_generated` flag, or its exhaustion notice).
visual_production_pipeline = Workflow(
    name="visual_production_pipeline",
    description="Generate visual concepts, then render their image creatives.",
    input_schema=PipelineRequest,
    edges=[
        (
            "START",
            visual_generation_pipeline,
            render_barrier,
            visual_generator_resilient,
            images_ready,
        )
    ],
)


# --- MAIN ORCHESTRATOR AGENT ---
root_agent = Agent(
    model=build_gemini_with_fallback(
        config.critic_model,
        config.critic_fallback_model,
        empty_turn_retries=ROOT_EMPTY_TURN_RETRIES,
    ),
    name="root_agent",
    retry_config=INFRA_RETRY,
    description="Help with ad generation; brainstorm and refine ad copy and visual concept ideas with actor-critic workflows; generate final ad creatives.",
    instruction=prompts.ROOT_AGENT_INSTR,
    tools=[
        combined_research_pipeline,
        ad_creative_pipeline,
        visual_production_pipeline,
        AgentTool(agent=creative_eval_agent),
        tools.save_eval_report_to_gcs,
        tools.save_draft_report_artifact,
        tools.save_creative_gallery_html,
        tools.write_trends_to_bq,
        tools.write_eval_report_to_bq,
        tools.memorize,
    ],
    generate_content_config=types.GenerateContentConfig(
        temperature=1.0,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "root_agent",
        },
    ),
    before_agent_callback=callbacks.load_session_state,
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
    after_agent_callback=callbacks.log_final_state_summary,
)

# Non-resumable App wrapper (creative_agent has no LongRunningFunctionTool
# checkpoints, so no ResumabilityConfig). It exists to carry App-level `plugins`:
# the opt-in Model Armor screen (empty unless MODEL_ARMOR_TEMPLATE is set), scoped
# to the root's own turns — see agent_common/safety.py. The runserver runner, the
# canned ADK loader, and deployment/deploy_agent.py (via AdkApp) all use this App.
app = App(
    name="creative_agent",
    root_agent=root_agent,
    plugins=build_safety_plugins(root_agent_names={root_agent.name}),
)
