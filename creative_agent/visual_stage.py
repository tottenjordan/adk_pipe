"""Visual stage of the creative pipeline: art director, concept drafter /
critic / finalizer, the deterministic concept gate with its bounded fixer
loop (``visual_generation_pipeline``) and the deterministic render node
(``visual_generator_resilient``), composed into
``visual_production_pipeline``.
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
from google.adk.workflow import FunctionNode, Workflow
from google.genai import types

from agent_common import FailSoftNode, PipelineRequest, build_gemini, is_populated
from agent_common.sampling import temperature as sampled_temperature

from . import callbacks, image_tools, prompts
from .concept_guard import (
    concept_issues,
    flatten_concept_issues,
    format_concept_issues,
)
from .config import INFRA_RETRY, SCHEMA_RETRY, config
from .copy_gate import CopyIssue
from .research_stage import item_count, missing_notice
from .run_policy import RunPolicy
from .schemas import (
    VisualConceptCritiqueList,
    VisualConceptFinalList,
    VisualConceptList,
)

# Every LlmAgent below sets mode="single_turn" explicitly (see the note in
# research_stage.py): graph nodes would otherwise default to "chat" mode and
# stall the graph on an empty model turn.


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
        temperature=sampled_temperature(0.9),
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "art_director",
        },
    ),
    output_key="visual_direction",
    # A fresh concept_gate fix budget / no stale issues per visual run.
    before_agent_callback=callbacks.reset_concept_state,
    before_model_callback=callbacks.rate_limit_callback,
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
        temperature=sampled_temperature(1.5),
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_concept_drafter",
        },
    ),
    output_schema=VisualConceptList,
    retry_config=SCHEMA_RETRY,
    output_key="visual_draft",
    before_model_callback=callbacks.rate_limit_callback,
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
        temperature=sampled_temperature(0.7),
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_concept_critic",
        },
    ),
    output_schema=VisualConceptCritiqueList,
    retry_config=SCHEMA_RETRY,
    output_key="visual_concept_critique",
    before_model_callback=callbacks.rate_limit_callback,
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
        temperature=sampled_temperature(0.8),
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_concept_finalizer",
        },
    ),
    output_schema=VisualConceptFinalList,
    retry_config=SCHEMA_RETRY,
    output_key="final_visual_concepts",
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
    # Deterministic last-line guard: every final image prompt names the concept's
    # trend_motif, the target product and its brand_cue (repairs + warns; runs
    # after the output_key write, so concept_gate and generate_image read the
    # repaired prompts).
    after_agent_callback=callbacks.ensure_trend_and_product_callback,
)


# --- VISUAL CONCEPT FIXER (bounded, targeted fix) --- #
# Rewrites ONLY the final visual concepts concept_gate flagged, fixing exactly
# their listed issues (`{visual_concept_issues?}`), and returns all concepts
# under the same output_key. Same worker bucket / schema / retry as the
# finalizer, plus the safety nets, in order: restore_unflagged_concepts_callback
# reverts any concept the gate did not flag (and restores dropped/duplicated
# ids) from the gate's pre-revision snapshot, THEN the trend/product/brand-cue
# guard repairs the fixed prompts.
visual_concept_fixer = Agent(
    model=build_gemini(config.worker_model),
    name="visual_concept_fixer",
    mode="single_turn",
    include_contents="none",
    description="Fixes only the flagged final visual concepts to clear the issues found by the concept gate",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.VISUAL_CONCEPT_FIXER_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=sampled_temperature(0.7),
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_concept_fixer",
        },
    ),
    output_schema=VisualConceptFinalList,
    retry_config=SCHEMA_RETRY,
    output_key="final_visual_concepts",
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=[
        callbacks.scrub_surrogates_in_response,
        callbacks.log_empty_turn_finish_reason,
    ],
    after_agent_callback=[
        callbacks.restore_unflagged_concepts_callback,
        callbacks.ensure_trend_and_product_callback,
    ],
)


# --- DETERMINISTIC CONCEPT GATE (bounded fix loop) --- #
# concept_gate runs creative_agent.concept_guard.concept_issues on the final
# visual concepts (after the trend/product/brand-cue guard): quoted in-image
# text matches the paired copy's headline/CTA (brand/product/brand-cue quotes
# allowed; meme/comic exempt), non-empty trend_motif, at most 2 concepts with
# quoted headline/CTA text, at most one centred hero —
# conservative string heuristics, all deterministic. Same contract as
# copy_gate: with issues and fix budget left (config.concept_revision_rounds,
# env CONCEPT_REVISION_ROUNDS, 0-2) it writes them as a Markdown list grouped
# per concept (`visual_concept_issues`, the fixer's input), the flagged keys
# and a pre-revision snapshot (the fixer's safety net), bumps the counter and
# routes "revise"; the fixer routes back to the gate, which re-checks. When the
# budget is spent, the issues left are recorded as
# `final_visual_concepts__issues` (surfaced by collect_degradation_warnings)
# and it routes "ok". Every "ok" exit clears the fix inputs.
#
# interactive_creative reuses visual_generation_pipeline (so it gets the gate
# before checkpoint 3). Its post-checkpoint path has no fix loop: a direct-edit
# resume clears final_visual_concepts__issues (runserver.async_runs), and the
# visual_concept_reviser, after ensure_trend_and_product_callback, re-records
# it on the revised concepts (callbacks.recheck_concept_issues_callback).
_CONCEPT_REVISION_CLEARED: dict[str, Any] = {
    "visual_concept_issues": "",
    "visual_concept_flagged_ids": None,
    "final_visual_concepts__before_revision": None,
}


def _concept_issues(state: Mapping[str, Any]) -> dict[str, list[CopyIssue]]:
    policy = RunPolicy.from_state(state, config)
    return concept_issues(
        state.get("final_visual_concepts"),
        state.get("ad_copy_critique"),
        brand=policy.brand,
        target_product=policy.target_product,
        strictness=policy.strictness,
        timely_ok=policy.timely_ok,
    )


def concept_gate_decision(
    state: Mapping[str, Any], max_rounds: int
) -> tuple[str, dict[str, Any]]:
    """The concept gate's (route, state_delta) for a state snapshot (pure)."""
    concepts = state.get("final_visual_concepts")
    issues = _concept_issues(state) if is_populated(concepts) else {}
    if not issues:
        return "ok", {
            **_CONCEPT_REVISION_CLEARED,
            "final_visual_concepts__issues": None,
        }
    used = int(state.get("visual_concept_revision_rounds_used") or 0)
    if used < max_rounds:
        return "revise", {
            "visual_concept_issues": format_concept_issues(concepts, issues),
            "visual_concept_flagged_ids": list(issues),
            "final_visual_concepts__before_revision": copy.deepcopy(concepts),
            "visual_concept_revision_rounds_used": used + 1,
        }
    return "ok", {
        **_CONCEPT_REVISION_CLEARED,
        "final_visual_concepts__issues": flatten_concept_issues(concepts, issues),
    }


def concept_gate(ctx: Context) -> Event:
    """Route flagged visual concepts to the fixer while budget remains."""
    route, delta = concept_gate_decision(
        ctx.state.to_dict(), config.concept_revision_rounds
    )
    if delta.get("final_visual_concepts__issues"):
        logging.warning(
            "visual concept issues remain: %s", delta["final_visual_concepts__issues"]
        )
    return Event(actions=EventActions(route=route, state_delta=delta))


# A raising fixer (e.g. SCHEMA_RETRY exhausted) must not fail the visual step:
# the finalizer's concepts are already in state. Keep the pre-revision
# concepts, record the gate's issues as final_visual_concepts__issues and spend
# the budget so the gate (which the failsoft node routes back to) does not
# re-run it.
def _visual_concept_fixer_failed(
    state: Mapping[str, Any], exc: Exception
) -> dict[str, Any]:
    before = state.get("final_visual_concepts__before_revision")
    concepts = before if is_populated(before) else state.get("final_visual_concepts")
    used = int(state.get("visual_concept_revision_rounds_used") or 0)
    issues = _concept_issues({**state, "final_visual_concepts": concepts})
    return {
        **_CONCEPT_REVISION_CLEARED,
        "final_visual_concepts": concepts,
        "final_visual_concepts__issues": flatten_concept_issues(concepts, issues)
        or None,
        "visual_concept_revision_rounds_used": max(
            used, config.concept_revision_rounds
        ),
    }


visual_concept_fixer_failsoft = FailSoftNode(
    name="visual_concept_fixer_failsoft",
    node=visual_concept_fixer,
    on_error=_visual_concept_fixer_failed,
)


# --- RENDER STEP (deterministic, no LLM) ---
# Runs generate_image over the finalized visual concepts. It used to be a Pro
# LlmAgent (visual_generator) whose only job was to call generate_image() with
# no arguments exactly once (~7.6 s of model time per run, plus a forced tool
# config and a retry-on-empty wrapper against MALFORMED_FUNCTION_CALL turns);
# a function node calls it directly. In creative_agent it is chained into
# visual_production_pipeline (below); interactive_creative invokes it as a
# separate root tool AFTER its visual review checkpoint (a bare node →
# NodeTool), hence the PipelineRequest input_schema, the explicit description
# and the kept public name.
async def _render_images(ctx: Context) -> str:
    """Render every final visual concept (generate_image on the node context).

    The node Context is the same class as ToolContext (state + save_artifact),
    so generate_image's writes (``_images_generated``, ``generated_images``,
    ``image_qa__issues``, ``person_reference_rejected``, …) land in the node's
    state delta. Its ``_images_generated`` guard keeps a re-run a no-op;
    transient infra exceptions are retried by the node's ``INFRA_RETRY`` and
    then propagate, as the agent's did. Looked up on the module at call time
    so tests can stub it.
    """
    result = await image_tools.generate_image(ctx)
    message = result.get("message") if isinstance(result, dict) else None
    return str(message or "Image creatives rendered.")


visual_generator_resilient = FunctionNode(
    func=_render_images,
    name="visual_generator_resilient",
    retry_config=INFRA_RETRY,
)
visual_generator_resilient.description = (
    "Generates the image creatives from the final visual concepts."
)
visual_generator_resilient.input_schema = PipelineRequest


def visual_concepts_ready(ctx: Context) -> Any:
    """Terminal node of visual_generation_pipeline (the root's tool result).

    A short confirmation, not the concepts themselves (see ad_copies_ready):
    the renderer, reviser and eval read `final_visual_concepts` from state. A
    non-empty notice when the finalizer produced none.
    """
    value = ctx.state.get("final_visual_concepts")
    if is_populated(value):
        return (
            f"Visual concepts complete: {item_count(value, 'visual_concepts')} "
            "concepts saved to session state as 'final_visual_concepts'."
        )
    return missing_notice("visual_concept_finalizer", "final_visual_concepts")


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
    step. A non-empty notice if the render step left ``_images_generated``
    unset (defensive: the deterministic node sets it or raises).
    """
    if is_populated(ctx.state.get("_images_generated")):
        keys = ctx.state.get("_generated_artifact_keys") or []
        return (
            f"Image creatives rendered: {len(keys)} image artifact(s) saved "
            "(keys in session state '_generated_artifact_keys')."
        )
    return missing_notice("visual_generator_resilient", "_images_generated")


# Graph for visual concepts (direction -> draft -> critique -> finalize ->
# deterministic concept gate with a bounded, targeted fix loop). Shared with
# interactive_creative, which pauses for human review after this stage before rendering.
visual_generation_pipeline = Workflow(
    name="visual_generation_pipeline",
    description="Generates visual concepts with an actor-critic workflow, then fixes concepts that fail the deterministic checks.",
    input_schema=PipelineRequest,
    edges=[
        (
            "START",
            art_director,
            visual_concept_drafter,
            visual_concept_critic,
            visual_concept_finalizer,
            concept_gate,
        ),
        (
            concept_gate,
            {"ok": visual_concepts_ready, "revise": visual_concept_fixer_failsoft},
        ),
        # The routed cycle: the gate's counter bounds the fixer passes.
        (visual_concept_fixer_failsoft, concept_gate),
    ],
)


# creative_agent (non-interactive) renders images immediately after finalizing
# concepts, as one deterministic unit. This removes the orchestrator's opportunity to
# skip image generation — which it once did, jumping straight from visual concepts to
# evaluation (now finalize_pipeline, the root's next call). interactive_creative does
# NOT use this: it keeps concepts and images split around a review checkpoint.
# Ends in images_ready, a short confirmation for the root (counted from state).
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
