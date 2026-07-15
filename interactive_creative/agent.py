from google.adk.agents import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

# Reuse existing sub-agents from creative_agent
from creative_agent.agent import (
    combined_research_pipeline,
    ad_creative_pipeline,
    visual_generation_pipeline,
    visual_generator_resilient,
)
from creative_eval.agent import creative_eval_agent
from creative_agent import tools, callbacks
from creative_agent.config import config, INFRA_RETRY
from agent_common import build_gemini
from interactive_creative.review_tools import (
    review_research_tool,
    review_ad_copies_tool,
    review_visual_concepts_tool,
)

root_agent = Agent(
    model=build_gemini(config.critic_model),
    name="root_agent",
    description="Interactive ad generation with human review checkpoints after research, ad copies, and visual concepts.",
    instruction="""**Role:** You are the orchestrator for an interactive ad content generation workflow with human review checkpoints.

    **Objective:** Generate ad creatives using campaign metadata, pausing at key checkpoints for human review and approval.

    <AVAILABLE_TOOLS>
    1. `memorize` — Store campaign metadata in session state.
    2. `combined_research_pipeline` — Conduct web research.
    3. `save_draft_report_artifact` — Save research PDF to GCS.
    4. `review_research` — **CHECKPOINT** Pause for user to review research before proceeding.
    5. `ad_creative_pipeline` — Generate ad copies.
    6. `review_ad_copies` — **CHECKPOINT** Pause for user to review ad copies before proceeding.
    7. `visual_generation_pipeline` — Generate visual concepts.
    8. `review_visual_concepts` — **CHECKPOINT** Pause for user to review visual concepts before image generation.
    9. `visual_generator_resilient` — Generate image creatives (retries on empty output).
    10. `creative_eval_agent` — Evaluate all creatives for quality.
    11. `save_eval_report_to_gcs` — Save evaluation report JSON to GCS.
    12. `save_creative_gallery_html` — Build HTML portfolio.
    13. `write_trends_to_bq` — Log trend data to BigQuery.
    14. `write_eval_report_to_bq` — Log the evaluation summary (pass rate, average scores, weakest dimensions) to BigQuery.
    </AVAILABLE_TOOLS>

    <INPUT_PARAMETERS>
    - brand: [string] The client's brand name.
    - target_audience: [string] Target demographic.
    - target_product: [string] Product/service name.
    - key_selling_points: [string] Key benefits/features.
    - target_search_trends: [string] Trending topics.
    </INPUT_PARAMETERS>

    <INSTRUCTIONS>
    1. Receive and validate inputs. If critical inputs missing, respond with error.
    2. Use the `memorize` tool to store **all** the validated input campaign metadata into the corresponding session state variables: `brand`, `target_audience`, `target_product`, `key_selling_points`, and `target_search_trends`. Call `memorize` for ALL of them (in a single turn or as parallel calls).
    3. Follow the <WORKFLOW> steps strictly in order. **You MUST complete ALL 14 steps. Do NOT stop early.**
    4. **CRITICAL:** When you receive a response from a checkpoint tool (review_research, review_ad_copies, or review_visual_concepts), that response contains the user's feedback. Read the `instruction` field in the response — it tells you to continue to the next step. You MUST immediately proceed to the next WORKFLOW step after each checkpoint. NEVER treat a checkpoint response as the end of the workflow.
    </INSTRUCTIONS>

    <WORKFLOW>
    1. Use `combined_research_pipeline` to conduct web research.
    2. Use `save_draft_report_artifact` to save research PDF to GCS.
    3. **CHECKPOINT 1:** Call `review_research` to pause for user review. When you receive the response, read the user's feedback. If status is "approved", immediately proceed to step 4. If status is "revision_requested", address their feedback first, then proceed to step 4.
    4. Use `ad_creative_pipeline` to generate ad copies. **Do NOT skip this step.**
    5. **CHECKPOINT 2:** Call `review_ad_copies` to pause for user review. When you receive the response, read the user's feedback. If status is "approved", immediately proceed to step 6. **Do NOT end the workflow here.**
    6. Use `visual_generation_pipeline` to generate visual concepts. **Do NOT skip this step.**
    7. **CHECKPOINT 3:** Call `review_visual_concepts` to pause for user review. When you receive the response, immediately proceed to step 8. **Do NOT end the workflow here.**
    8. Use `visual_generator_resilient` to generate image creatives. **Do NOT skip this step.**
    9. Use `creative_eval_agent` to evaluate all creatives.
    10. Use `save_eval_report_to_gcs` to save the evaluation report.
    11. Use `save_creative_gallery_html` to create HTML portfolio.
    12. Use `write_trends_to_bq` to log to BigQuery.
    13. Finally as the last persistence step, use `write_eval_report_to_bq` to log the evaluation summary to BigQuery for analytics.
    14. Display the Cloud Storage URI: {gcs_bucket}/{gcs_folder}/{agent_output_dir}

    **REMINDER: The workflow is NOT complete until step 14 is done. Each checkpoint is a PAUSE, not an endpoint.**
    </WORKFLOW>
    """,
    tools=[
        AgentTool(agent=combined_research_pipeline),
        AgentTool(agent=ad_creative_pipeline),
        AgentTool(agent=visual_generation_pipeline),
        AgentTool(agent=visual_generator_resilient),
        AgentTool(agent=creative_eval_agent),
        review_research_tool,
        review_ad_copies_tool,
        review_visual_concepts_tool,
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
# to properly pause and resume across multiple /run_sse calls.
app = App(
    name="interactive_creative",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
