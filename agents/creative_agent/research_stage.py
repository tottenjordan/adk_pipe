"""Research stage of the creative pipeline: parallel campaign + trend
research, an optional refinement round, the cited report, the structured
creative brief with its deterministic gate (bounded revision loop) and the
research PDF, composed into ``combined_research_pipeline``.

Also hosts the shared pipeline-result helpers (``missing_notice``,
``item_count``) that every stage's truthy terminal node uses.
"""

import json
import logging
from collections.abc import Mapping
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.planners import BuiltInPlanner
from google.adk.tools import google_search
from google.adk.workflow import JoinNode, Workflow
from google.genai import types

from agent_common import (
    FailSoftNode,
    PipelineRequest,
    RetryUntilKeyNode,
    build_gemini,
    build_gemini_with_fallback,
    is_populated,
)
from agent_common.sampling import temperature as sampled_temperature

from . import brand_history, callbacks, gcs_tools, prompts, safer_angle
from .brief_check import check_brief, normalize_brief_terms, parse_brief
from .brief_render import render_brief_markdown
from .config import SCHEMA_RETRY, config
from .finalize import issue_message
from .run_policy import RunPolicy
from .safer_angle import clear_stray_safer_angle
from .schemas import CreativeBrief, ResearchFeedback
from .sub_agents.campaign_researcher.agent import ca_sequential_planner
from .sub_agents.trend_researcher.agent import gs_sequential_planner
from .sub_agents.trend_situation.agent import trend_situation_pipeline

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
            temperature=sampled_temperature(0.7),
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
# large CreativeBrief schema — RetryUntilKeyNode only retries empty output and transient errors) must
# not fail the research step. FailSoftNode logs it and applies these deltas:
# - writer: no brief + creative_brief__retry_exhausted (same degradation as an
#   empty-output exhaustion; the creatives fall back to the report);
# - reviser: keep the pre-revision brief, record the gate's issues as
#   creative_brief__issues, and spend the revision budget (a raising reviser is
#   not re-run by the gate).
def _current_moment(state: Mapping[str, Any]) -> str:
    """The validated current moment line (``trend_situation_md``; "" if none)."""
    value = state.get("trend_situation_md")
    return value if isinstance(value, str) else ""


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
            state.get("creative_brief"),
            heading=False,
            current_moment=_current_moment(state),
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
    return check_brief(state.get("creative_brief"), RunPolicy.from_state(state, config))


def brief_gate_decision(
    state: Mapping[str, Any], max_rounds: int
) -> tuple[str, dict[str, Any]]:
    """The brief gate's (route, state_delta) for a state snapshot (pure)."""
    brief = state.get("creative_brief")
    if not is_populated(brief):
        return "ok", {"brief_issues": "", "creative_brief_md": ""}
    # Clean the term lists at the source (e.g. "[wrestler likenesses]" copied
    # from a placeholder) so every consumer — copy gate, prompts, judge, UI —
    # sees plain terms; the cleaned brief is written back on every exit.
    snapshot: dict[str, Any] = dict(state)
    base: dict[str, Any] = {}
    cleaned = normalize_brief_terms(brief)
    if cleaned is not None and cleaned != dict(parse_brief(brief) or {}):
        brief = cleaned
        snapshot["creative_brief"] = cleaned
        base["creative_brief"] = cleaned
    # Without an active safer angle the creative prompts must never see one:
    # a stray trend_bridge.safer_angle is cleared here (no revision round).
    if not safer_angle.is_active(snapshot) and (
        stray := clear_stray_safer_angle(dict(parse_brief(brief) or {}))
    ):
        brief = stray
        snapshot["creative_brief"] = stray
        base["creative_brief"] = stray
    brief_md = render_brief_markdown(
        brief, heading=False, current_moment=_current_moment(snapshot)
    )
    base["creative_brief_md"] = brief_md
    issues = _brief_issues(snapshot)
    if not issues:
        return "ok", {
            **base,
            "brief_issues": "",
            "creative_brief__issues": None,
        }
    used = int(snapshot.get("brief_revision_rounds_used") or 0)
    if used < max_rounds:
        return "revise", {
            **base,
            "brief_issues": "\n".join(f"- {issue}" for issue in issues),
            "brief_revision_rounds_used": used + 1,
        }
    return "ok", {
        **base,
        "brief_issues": "",
        "creative_brief__issues": issues,
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


async def load_brand_history(ctx: Context) -> None:
    """Read the brand's past runs into `brand_history` (no LLM, fail-soft).

    Runs in the START fan-out next to the two research planners and feeds the
    JoinNode, so its BigQuery/GCS read (bounded to ~10 s, in a worker thread)
    overlaps the research instead of delaying it. Writes the note the brief
    writer and art director read via `{brand_history?}` ("" without history)
    and, unless the user set a style preference, re-draws `style_shortlist`
    without the brand's recently used styles. Disabled by
    BRAND_HISTORY_ENABLED=false / BRAND_HISTORY_RUNS=0 (state untouched).
    When the run opted in (`learn_from_ratings`) and RATING_LEARNING_ENABLED is
    on, it also reads the brand's human ratings concurrently (same bound) into
    `rating_signals` / `rating_strictness` / `rating_signals_applied` and
    steers the same single shortlist draw (creative_agent/rating_signals.py).
    Yields no output (the barrier drops the join dict anyway).
    """
    try:
        delta = await brand_history.brand_history_state_delta(
            ctx.state.to_dict(),
            enabled=config.brand_history_enabled,
            runs=config.brand_history_runs,
        )
    except Exception:
        logging.exception("brand history step failed; continuing without it")
        return None
    for key, value in delta.items():
        ctx.state[key] = value
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


def missing_notice(producer: str, key: str) -> str:
    return (
        f"{producer} did not produce '{key}'; it is unavailable for this run. "
        "Continue with the next workflow step."
    )


def item_count(value: Any, list_key: str) -> int:
    """Length of ``value[list_key]`` for a dict or JSON-string payload; 0 if absent."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return 0
    items = value.get(list_key) if isinstance(value, dict) else None
    return len(items) if isinstance(items, list) else 0


async def save_research_pdf_node(ctx: Context) -> None:
    """Save the cited research report (with the brief) as a PDF artifact + to GCS.

    Runs on brief_gate's "ok" exit, so the PDF carries the final brief; it was a
    separate root tool call (save_draft_report_artifact) before. Skipped when
    the composer produced no report (research_report_ready already says so). A
    failure is logged and recorded as ``research_report_gcs_uri__issues``, never
    raised: the PDF is a deliverable, not an input to the creative stages.
    """
    if not is_populated(ctx.state.get("final_report_with_citations")):
        logging.warning("research PDF skipped: no final_report_with_citations")
        return None
    try:
        await gcs_tools.save_draft_report_artifact(ctx)
    except Exception as exc:
        logging.exception("research PDF save failed")
        ctx.state["research_report_gcs_uri__issues"] = issue_message(
            "research PDF", exc
        )
    return None


def research_report_ready(ctx: Context) -> str:
    """Terminal node of combined_research_pipeline (the root's tool result).

    A short confirmation, not the report itself, mirroring the pre-graph
    AgentTool result (the composer's citation-callback text): the report lives
    in state for the creative stages (and was saved as a PDF by
    save_research_pdf_node), and repeating it in the root's context would only
    add tokens.
    """
    brief_note = (
        " Structured creative brief saved as 'creative_brief'."
        if is_populated(ctx.state.get("creative_brief"))
        else " No structured creative brief is available for this run."
    )
    pdf_uri = ctx.state.get("research_report_gcs_uri")
    pdf_note = f" Research PDF saved to {pdf_uri}." if is_populated(pdf_uri) else ""
    if is_populated(ctx.state.get("combined_final_cited_report")):
        return (
            "Research report complete: saved to session state as "
            "'combined_final_cited_report' (with resolved citations in "
            "'final_report_with_citations')." + brief_note + pdf_note
        )
    return (
        missing_notice("combined_report_composer", "combined_final_cited_report")
        + brief_note
    )


# --- COMPLETE RESEARCH PIPELINE --- #
# Graph: both research chains fan out from START and run concurrently, a
# JoinNode waits for both, the barrier drops the join dict, merge_planners
# synthesizes the base brief, and the gate routes either through the refinement
# round (degraded research) or straight to the composer (healthy path). The
# composer's report is then distilled into the structured creative brief, which
# brief_gate either accepts or sends through a bounded revision loop; on accept
# the report (with the brief) is saved as the research PDF. The fan-out also
# runs load_brand_history and the trend situation branch (the trend's validated
# current moment, sub_agents/trend_situation), so the join waits for all four.
combined_research_pipeline = Workflow(
    name="combined_research_pipeline",
    description="Runs parallel campaign + trend research, a refinement round only when that research is degraded, then a cited report and a structured creative brief.",
    input_schema=PipelineRequest,
    edges=[
        (
            "START",
            (
                gs_sequential_planner,
                ca_sequential_planner,
                load_brand_history,
                trend_situation_pipeline,
            ),
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
        (brief_gate, {"ok": save_research_pdf_node, "revise": brief_reviser_failsoft}),
        (save_research_pdf_node, research_report_ready),
        # The routed cycle: the gate's counter bounds the reviser passes.
        (brief_reviser_failsoft, brief_gate),
    ],
)
