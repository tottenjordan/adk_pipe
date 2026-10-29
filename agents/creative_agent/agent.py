"""creative_agent's composition root: the single-call ``creative_pipeline``
(research -> ad copy -> visual production, with the early ad-copy judging
fan-out, then finalize), the root agent and its ``App``.

The stages live in ``research_stage``, ``copy_stage`` and ``visual_stage``;
the names below are re-exported so ``creative_agent.agent.<name>`` (tests, the
facade in ``__init__``) keeps working.
"""

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.workflow import FunctionNode, JoinNode, Workflow
from google.genai import types

from agent_common import (
    ROOT_EMPTY_TURN_RETRIES,
    PipelineRequest,
    build_gemini_with_fallback,
    build_safety_plugins,
    drop_other_agent_context,
)
from agent_common.sampling import temperature as sampled_temperature

from . import callbacks, prompts, tools
from .config import INFRA_RETRY, config
from .copy_stage import (  # noqa: F401  (re-exported)
    ad_copies_ready,
    ad_copy_critic,
    ad_copy_drafter,
    ad_copy_reviser,
    ad_copy_reviser_failsoft,
    ad_creative_pipeline,
    cap_and_residual_copies,
    copy_gate,
    copy_gate_decision,
    residual_copy_issues,
)
from .finalize import (
    evaluate_ad_copies_node,
    evaluate_creatives_node,
    finalize_ready,
    persist_node,
)
from .research_stage import (  # noqa: F401  (re-exported)
    brief_gate,
    brief_gate_decision,
    brief_reviser,
    brief_writer,
    combined_report_composer,
    combined_research_pipeline,
    combined_web_evaluator,
    describe_reference_assets,
    enhanced_combined_searcher,
    enterprise_sequential_planner,
    load_brand_history,
    merge_planners,
    refined_web_synthesizer,
    refinement_gate,
    refinement_gate_route,
    research_report_ready,
    save_research_pdf_node,
)
from .schemas import (  # noqa: F401
    AdCopy,
    AdCopyList,
    BrandCues,
    BriefCheck,
    CompositionMode,
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
from .visual_stage import (  # noqa: F401  (re-exported)
    art_director,
    concept_gate,
    concept_gate_decision,
    images_ready,
    render_barrier,
    visual_concept_critic,
    visual_concept_drafter,
    visual_concept_finalizer,
    visual_concept_fixer,
    visual_concept_fixer_failsoft,
    visual_concepts_ready,
    visual_generation_pipeline,
    visual_generator_resilient,
    visual_production_pipeline,
)

# --- FINALIZE (evaluate -> persist -> summary) --- #
# The post-render steps as one deterministic unit (logic in creative_agent/
# finalize.py): the LLM judge scores every creative, then the eval report +
# HTML gallery are saved to GCS and the trend_creatives + creative_evals rows
# written to BigQuery. Each step is fail-soft (degradation markers, never
# raises), and finalize_ready returns the root a compact, always-truthy summary.
# This replaces five separate root tool decisions (the creative_eval_agent
# AgentTool + four persistence tools) the Pro root could end early between.
finalize_pipeline = Workflow(
    name="finalize_pipeline",
    description="Evaluates all creatives and saves the evaluation report, HTML gallery and BigQuery rows.",
    input_schema=PipelineRequest,
    edges=[("START", evaluate_creatives_node, persist_node, finalize_ready)],
)


# --- SINGLE-CALL CREATIVE PIPELINE --- #
# The root's only workflow call: every stage chained as a nested Workflow, so
# the run no longer depends on the Pro root making four consecutive tool
# decisions. It ended Agent Engine runs with empty turns between them (seen
# 2026-10-07 after visual_production_pipeline: finalize never ran, so no eval
# report, gallery or eval BQ row), and Agent Engine has no runserver
# auto-continue to recover. interactive_creative still calls the stages
# separately, around its review checkpoints.


def _no_output() -> None:
    """Drop the previous stage's confirmation string (see _stage_barrier)."""
    return None


def _stage_barrier(stage: Workflow) -> FunctionNode:
    """A no-output pass-through in front of a nested stage (render_barrier pattern).

    Every stage validates its input against PipelineRequest (each is also a
    root tool), so the previous stage's truthy confirmation string must not
    reach it; the stages read their inputs from state anyway.
    """
    return FunctionNode(
        func=_no_output, name=f"{stage.name.removesuffix('_pipeline')}_barrier"
    )


# After the ad copies, the graph fans out: the visual stage (concepts + renders,
# ~4 min) runs next to evaluate_ad_copies_node, which judges the ad copies in the
# meantime (Pro judge calls overlapping flash/image work, not the visual judge).
# The JoinNode waits for both; finalize's evaluate step then judges only the
# visuals and merges the stored ad-copy results (or judges everything itself if
# the early evaluation failed or is stale).
ad_copy_eval_join = JoinNode(name="ad_copy_eval_join")
_visual_production_barrier = _stage_barrier(visual_production_pipeline)

creative_pipeline = Workflow(
    name="creative_pipeline",
    description="Runs the complete creative workflow: research and a creative brief (research PDF saved), ad copies, visual concepts with rendered images, then evaluation and export of the report, HTML gallery and BigQuery rows. Returns the evaluation summary.",
    input_schema=PipelineRequest,
    edges=[
        (
            "START",
            combined_research_pipeline,
            _stage_barrier(ad_creative_pipeline),
            ad_creative_pipeline,
            (_visual_production_barrier, evaluate_ad_copies_node),
        ),
        (_visual_production_barrier, visual_production_pipeline, ad_copy_eval_join),
        (evaluate_ad_copies_node, ad_copy_eval_join),
        (
            ad_copy_eval_join,
            # Drops the join dict (finalize_pipeline validates PipelineRequest).
            _stage_barrier(finalize_pipeline),
            # Terminal: finalize_ready's always-truthy summary.
            finalize_pipeline,
        ),
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
    description="Generates trend-based ad creatives: records the campaign inputs, then runs the creative_pipeline once (research and creative brief, gated ad copies, visual concepts with rendered images, evaluation and export).",
    instruction=prompts.ROOT_AGENT_INSTR,
    tools=[creative_pipeline, tools.memorize],
    generate_content_config=types.GenerateContentConfig(
        temperature=sampled_temperature(1.0),
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "root_agent",
        },
    ),
    before_agent_callback=callbacks.load_session_state,
    # Drop the pipelines' replayed sub-agent turns + node inputs first (they
    # bloated the root's prompt; see agent_common/history.py), then rate-limit.
    before_model_callback=[drop_other_agent_context, callbacks.rate_limit_callback],
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
