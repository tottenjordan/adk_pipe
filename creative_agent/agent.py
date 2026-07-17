import logging
import warnings

from google.genai import types
from google.adk.tools import google_search
from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.agents import Agent, SequentialAgent, ParallelAgent

from .sub_agents.campaign_researcher.agent import ca_sequential_planner
from .sub_agents.trend_researcher.agent import gs_sequential_planner
from agent_common import build_gemini, RetryUntilKeyAgent, RunIfAgent
from .config import config, INFRA_RETRY, SCHEMA_RETRY
from . import callbacks
from . import tools
from . import prompts
from .schemas import (  # noqa: F401
    SearchQuery,
    ResearchFeedback,
    AdCopy,
    AdCopyList,
    FinalAdCopy,
    FinalAdCopyList,
    VisualConcept,
    VisualConceptList,
    VisualConceptCritique,
    VisualConceptCritiqueList,
    VisualConceptFinal,
    VisualConceptFinalList,
)
from creative_eval.agent import creative_eval_agent


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


# --- PARALLEL RESEARCH SUBAGENTS --- #
parallel_planner_agent = ParallelAgent(
    name="parallel_planner_agent",
    sub_agents=[gs_sequential_planner, ca_sequential_planner],
    description="Runs multiple research planning agents in parallel.",
)

merge_planners = Agent(
    name="merge_planners",
    model=build_gemini(config.worker_model),
    include_contents="none",
    description="Combine results from state keys 'campaign_web_search_insights' and 'gs_web_search_insights'",
    instruction=prompts.MERGE_PLANNERS_INSTR,
    output_key="combined_web_search_insights",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


merge_parallel_insights = SequentialAgent(
    name="merge_parallel_insights",
    sub_agents=[parallel_planner_agent, merge_planners],
    description="Coordinates parallel research and synthesizes the results.",
)


combined_web_evaluator = Agent(
    model=build_gemini(config.critic_model),
    name="combined_web_evaluator",
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
    include_contents="none",
    description="Synthesizes the raw follow-up findings into a concise new-insights summary.",
    instruction=prompts.REFINED_WEB_SYNTHESIZER_INSTR,
    output_key="refined_web_search_insights",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)

refined_search_and_synthesize = SequentialAgent(
    name="refined_search_and_synthesize",
    description="Runs the follow-up web search then synthesizes the new findings.",
    sub_agents=[enhanced_combined_searcher, refined_web_synthesizer],
)


# Retry-on-empty: if the searcher OR synthesizer emits no final text (leaving
# `refined_web_search_insights` unset), re-run the whole pair until populated
# (bounded). combined_report_composer already guards with
# `{refined_web_search_insights?}`, but retrying recovers the refinement (a
# quality gain) instead of silently dropping it. The wrapper runs only
# sub_agents[0], so we wrap the SequentialAgent pair.
enhanced_combined_searcher_resilient = RetryUntilKeyAgent(
    name="enhanced_combined_searcher_resilient",
    sub_agents=[refined_search_and_synthesize],
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
    model=build_gemini(config.critic_model),
    name="combined_report_composer",
    include_contents="none",
    description="Transforms research data and a markdown outline into a final, cited report.",
    instruction=prompts.COMBINED_REPORT_COMPOSER_INSTR,
    output_key="combined_final_cited_report",
    after_agent_callback=callbacks.citation_replacement_callback,
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)

# 5.  **## Final Risk Assessment & Constraints**
#     *   (Introductory Paragraph: Summary of any critical constraints or risks the creative team must avoid.)
#     *   (No more than 3 supporting bullets detailing the specific risks/constraints.)


# --- CONDITIONAL RESEARCH REFINEMENT GATE (Lever A) --- #
# The evaluator (gemini-3.1-pro-preview) + follow-up searcher form a SECOND,
# additive research round: the base brief in `combined_web_search_insights`
# (two parallel deep researchers → synthesis) already flows straight to
# `combined_report_composer`, which guards the refined input with the optional
# `{refined_web_search_insights?}`. So the refinement is only *worth* an extra
# serial PRO call when the base research came back thin.
#
# `_base_research_is_degraded` gates the block on exactly that: run it only when
# the merged brief is blank/missing, or an upstream producer exhausted its
# retries (`*__retry_exhausted`, set by the RetryUntilKeyAgent wrappers on the
# gs/campaign producers). On the healthy common path the gate skips the block —
# dropping one gemini-3.1-pro-preview call (the 5 RPM quota is the wall-clock
# bottleneck) plus a google_search + synthesis pass — while keeping the round as
# a self-healing fallback for degraded runs. No `output_key`/`{var?}` guard is
# disturbed: the evaluator's output is consumed only inside the block, and the
# composer already tolerates a missing `refined_web_search_insights`.
def _base_research_is_degraded(state) -> bool:
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


research_refinement_block = RunIfAgent(
    name="research_refinement_block",
    description="Runs the follow-up evaluate+search round only when base research is degraded.",
    predicate=_base_research_is_degraded,
    sub_agents=[
        combined_web_evaluator,
        enhanced_combined_searcher_resilient,
    ],
)


# --- COMPLETE RESEARCH PIPELINE SUBAGENT --- #
combined_research_pipeline = SequentialAgent(
    name="combined_research_pipeline",
    description="Executes a pipeline of web research. It performs iterative research, evaluation, and insight generation.",
    sub_agents=[
        merge_parallel_insights,
        research_refinement_block,
        combined_report_composer,
    ],
)


# --- AD COPY AGENT (DRAFT) ---
ad_copy_drafter = Agent(
    model=build_gemini(config.worker_model),
    name="ad_copy_drafter",
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


# Sequential agent for ad creative generation
ad_creative_pipeline = SequentialAgent(
    name="ad_creative_pipeline",
    description="Generates ad copy drafts with an actor-critic workflow.",
    sub_agents=[
        ad_copy_drafter,
        ad_copy_critic,
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
    include_contents="none",
    description="Critique and narrow down visual concepts",
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
)


# --- VISUAL GENERATOR AGENT ---
# Runs generate_image over the finalized visual concepts. In creative_agent it is
# chained into visual_production_pipeline (below) so image rendering is deterministic
# and the orchestrator cannot skip it. interactive_creative deliberately invokes it as
# a separate step AFTER a human review checkpoint (review concepts before spending on
# image generation), so it must also remain usable as a standalone agent.
visual_generator = Agent(
    model=build_gemini(config.critic_model),
    name="visual_generator",
    retry_config=INFRA_RETRY,
    include_contents="none",  # new
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
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# Retry-on-empty for the image step: visual_generator (gemini-3.1-pro-preview)
# intermittently returns MALFORMED_FUNCTION_CALL and never emits the generate_image
# tool call, leaving _images_generated unset and shipping an empty gallery (run
# 2032568396381421568). retry_config=INFRA_RETRY only covers infra EXCEPTIONS, not a
# malformed-call finish reason — so wrap in RetryUntilKeyAgent (same pattern as the
# research producers), keyed on the _images_generated flag generate_image already sets
# on success. That flag also makes a re-run safe (idempotency guard → no double image
# spend); on exhaustion the wrapper emits _images_generated__retry_exhausted, which
# collect_degradation_warnings surfaces on the gallery/eval banner. Single shared
# instance (also used by interactive_creative via AgentTool) to avoid double-parenting.
visual_generator_resilient = RetryUntilKeyAgent(
    name="visual_generator_resilient",
    sub_agents=[visual_generator],
    output_key="_images_generated",
    max_attempts=3,
)


# Sequential agent for visual concepts (draft -> critique -> finalize). Shared with
# interactive_creative, which pauses for human review after this stage before rendering.
visual_generation_pipeline = SequentialAgent(
    name="visual_generation_pipeline",
    description="Generates visual concepts with an actor-critic workflow.",
    sub_agents=[
        art_director,
        visual_concept_drafter,
        visual_concept_critic,
        visual_concept_finalizer,
    ],
)


# creative_agent (non-interactive) renders images immediately after finalizing
# concepts, as one deterministic unit. This removes the orchestrator's opportunity to
# skip image generation — which it did when creative_eval_agent looked like the next
# step, jumping straight from visual concepts to evaluation. interactive_creative does
# NOT use this: it keeps concepts and images split around a review checkpoint.
visual_production_pipeline = SequentialAgent(
    name="visual_production_pipeline",
    description="Generate visual concepts, then render their image creatives.",
    sub_agents=[
        visual_generation_pipeline,
        visual_generator_resilient,
    ],
)


# --- MAIN ORCHESTRATOR AGENT ---
root_agent = Agent(
    model=build_gemini(config.critic_model),
    name="root_agent",
    retry_config=INFRA_RETRY,
    description="Help with ad generation; brainstorm and refine ad copy and visual concept ideas with actor-critic workflows; generate final ad creatives.",
    instruction=prompts.ROOT_AGENT_INSTR,
    tools=[
        AgentTool(agent=combined_research_pipeline),
        AgentTool(agent=ad_creative_pipeline),
        AgentTool(agent=visual_production_pipeline),
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

# To ensure correct state management, **chain the calls** such that you only call the next `memorize` after the previous call has successfully responded.
