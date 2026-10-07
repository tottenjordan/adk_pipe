from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.tools.agent_tool import AgentTool
from google.adk.workflow import Workflow
from google.genai import types

from agent_common import (
    ROOT_EMPTY_TURN_RETRIES,
    FailSoftNode,
    PipelineRequest,
    build_gemini,
    build_gemini_with_fallback,
    build_safety_plugins,
    drop_other_agent_context,
)

# Reuse existing building blocks from the creative_agent public facade.
from creative_agent import (
    VisualConceptFinalList,
    ad_copy_reviser,
    ad_creative_pipeline,
    callbacks,
    combined_research_pipeline,
    finalize_pipeline,
    tools,
    visual_generation_pipeline,
    visual_generator_resilient,
)
from creative_agent.config import INFRA_RETRY, SCHEMA_RETRY, config
from interactive_creative import prompts as ic_prompts
from interactive_creative.callbacks import (
    USER_REVISER_TOOL,
    USER_REVISION_FAILED_KEY,
    clear_copy_revision_inputs,
    skip_reviser_without_notes,
    user_copy_revision_failed,
)
from interactive_creative.review_tools import (
    prepare_copy_revision,
    review_ad_copies_tool,
    review_research_tool,
    review_visual_concepts_tool,
)

# --- VISUAL CONCEPT REVISER (interactive-only) ---
# At checkpoint 3 the user can (a) directly edit concept fields — merged
# deterministically into `final_visual_concepts` state on resume (see
# runserver.async_runs.merge_visual_concept_edits) — and (b) leave free-text
# revision notes. This LLM step applies the natural-language notes to the matching
# concepts' image_generation_prompt and re-emits `final_visual_concepts` BEFORE the
# renderer reads it. It is a structured-output producer mirroring
# visual_concept_finalizer (include_contents="none", output_schema + SCHEMA_RETRY),
# and runs as its own AgentTool step so it does NOT re-parent the shared
# visual_generator_resilient (which would double-parent — see that agent's note).
visual_concept_reviser = Agent(
    model=build_gemini(config.worker_model),
    name="visual_concept_reviser",
    include_contents="none",
    description="Apply the user's checkpoint revision notes to the finalized visual concepts before rendering.",
    instruction=ic_prompts.VISUAL_CONCEPT_REVISER_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.4,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "interactive_creative",
            "subagent": "visual_concept_reviser",
        },
    ),
    output_schema=VisualConceptFinalList,
    retry_config=SCHEMA_RETRY,
    output_key="final_visual_concepts",
    # Skip the model when there are no notes (or no concepts): with nothing to
    # apply, an LLM re-emit could only paraphrase away the merged direct edits.
    before_agent_callback=skip_reviser_without_notes,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
    # Same trend-motif + product guard as visual_concept_finalizer: a revision
    # note must not drop either from the prompt that is rendered. Then the
    # deterministic concept checks are re-run on the guarded concepts so
    # final_visual_concepts__issues reflects them, not the pre-checkpoint
    # concept_gate verdict (record only, no fix loop). (Neither is reached on
    # the skip path: a before_agent reply ends the reviser's invocation; the
    # echoed concepts were already guarded, and a direct-edit resume cleared
    # the stale marker in runserver.async_runs._apply_visual_concept_edits.)
    after_agent_callback=[
        callbacks.ensure_trend_and_product_callback,
        callbacks.recheck_concept_issues_callback,
    ],
)


# --- CHECKPOINT-2 USER REVISION (interactive-only) ---
# A checkpoint-2 revision request with feedback revises the copies ONCE:
# prepare_copy_revision flags every copy with the user's feedback (writing the
# reviser's ad_copy_issues / ad_copy_flagged_ids / pre-revision snapshot), then
# this small graph runs creative_agent's shared ad_copy_reviser (the copy gate's
# reviser, from the facade). The reviser is a mode="single_turn" agent, which
# AgentTool cannot run as its child-runner root, so it runs as a graph node
# (bare Workflow → NodeTool, like the pipelines). FailSoftNode keeps a raising
# reviser from failing the run (the pre-revision copies stay in state), and the
# truthy terminal hands the root a short confirmation instead of the copies
# JSON. The root's after_tool_callback clears the revision inputs afterwards.
def ad_copies_revised(ctx: Context) -> str:
    """Terminal node of ad_copy_user_reviser (the root's tool result)."""
    if ctx.state.get(USER_REVISION_FAILED_KEY):
        return (
            "Ad copy revision failed; the original copies are kept in "
            "'ad_copy_critique'. Call review_ad_copies once more."
        )
    return (
        "Ad copy revision complete: the revised copies are saved to session "
        "state as 'ad_copy_critique'. Call review_ad_copies once more."
    )


ad_copy_user_reviser = Workflow(
    name=USER_REVISER_TOOL,
    description="Revise the ad copies with the user's checkpoint-2 feedback (call only after prepare_copy_revision returns status 'ready').",
    input_schema=PipelineRequest,
    edges=[
        (
            "START",
            FailSoftNode(
                name="ad_copy_user_reviser_failsoft",
                node=ad_copy_reviser,
                on_error=user_copy_revision_failed,
            ),
            ad_copies_revised,
        ),
    ],
)

root_agent = Agent(
    model=build_gemini_with_fallback(
        config.critic_model,
        config.critic_fallback_model,
        empty_turn_retries=ROOT_EMPTY_TURN_RETRIES,
    ),
    name="root_agent",
    description="Interactive ad generation with human review checkpoints after research, ad copies, and visual concepts.",
    instruction=ic_prompts.ROOT_AGENT_INSTR,
    tools=[
        combined_research_pipeline,
        ad_creative_pipeline,
        visual_generation_pipeline,
        AgentTool(agent=visual_concept_reviser),
        # Checkpoint-2 user revision (see ad_copy_user_reviser above).
        prepare_copy_revision,
        ad_copy_user_reviser,
        visual_generator_resilient,
        finalize_pipeline,
        review_research_tool,
        review_ad_copies_tool,
        review_visual_concepts_tool,
        # The research pipeline saves the PDF itself; the root re-saves it only
        # when the user edited the brief or report at checkpoint 1
        # (brief_edited / report_edited).
        tools.save_draft_report_artifact,
        tools.memorize,
    ],
    generate_content_config=types.GenerateContentConfig(
        temperature=1.0,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "interactive_creative",
            "subagent": "root_agent",
        },
    ),
    before_agent_callback=callbacks.load_session_state,
    # Drop the pipelines' replayed sub-agent turns + node inputs first (they
    # bloated the root's prompt; see agent_common/history.py), then rate-limit.
    before_model_callback=[drop_other_agent_context, callbacks.rate_limit_callback],
    after_model_callback=callbacks.log_empty_turn_finish_reason,
    # Checkpoint-2 revision bookkeeping: clear the reviser's inputs after it runs.
    after_tool_callback=clear_copy_revision_inputs,
    after_agent_callback=callbacks.log_final_state_summary,
    retry_config=INFRA_RETRY,
)

# Wrap in App with resumability enabled — required for LongRunningFunctionTool
# to properly pause and resume across multiple /run_sse calls. `plugins` is the
# opt-in Model Armor screen (empty unless MODEL_ARMOR_TEMPLATE is set), scoped to
# the root's own turns — see agent_common/safety.py.
app = App(
    name="interactive_creative",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
    plugins=build_safety_plugins(root_agent_names={root_agent.name}),
)
