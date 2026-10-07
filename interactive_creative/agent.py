from google.adk.agents import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from agent_common import (
    ROOT_EMPTY_TURN_RETRIES,
    build_gemini,
    build_gemini_with_fallback,
    build_safety_plugins,
)

# Reuse existing building blocks from the creative_agent public facade.
from creative_agent import (
    VisualConceptFinalList,
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
from interactive_creative.callbacks import skip_reviser_without_notes
from interactive_creative.review_tools import (
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
        visual_generator_resilient,
        finalize_pipeline,
        review_research_tool,
        review_ad_copies_tool,
        review_visual_concepts_tool,
        # The research pipeline saves the PDF itself; the root re-saves it only
        # when the user edited the report at checkpoint 1 (report_edited).
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
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
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
