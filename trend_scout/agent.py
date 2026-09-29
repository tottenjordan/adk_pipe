import logging

from google.adk.agents import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.planners import BuiltInPlanner
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from google.adk.workflow import Workflow
from google.genai import types

from agent_common import (
    PipelineRequest,
    RetryUntilKeyNode,
    build_gemini,
    build_gemini_with_fallback,
    build_safety_plugins,
)

from . import callbacks, prompts
from .config import INFRA_RETRY, config
from .review_tools import review_trends_tool
from .tools import (
    get_daily_gtrends,
    memorize,
    record_research_gaps,
    save_search_trends_to_session_state,
    save_session_state_to_gcs,
    write_to_file,
    write_trends_to_bq,
)

# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


# --- TREND SUBAGENTS ---
gather_trends_agent = Agent(
    # Trivial tool-output formatting; runs on its own global bucket (gather_model).
    model=build_gemini(config.gather_model),
    name="gather_trends_agent",
    include_contents="none",
    description="Get top 25 trending terms from Google Search.",
    instruction=prompts.GATHER_TRENDS_INSTR,
    tools=[get_daily_gtrends],
    retry_config=INFRA_RETRY,
    generate_content_config=types.GenerateContentConfig(
        temperature=1.0,
        response_modalities=["TEXT"],
        labels={
            "agentic_wf": "trend_scout",
            "agent": "trend_scout",
            "subagent": "gather_trends_agent",
        },
    ),
    # output_key="start_gtrends",
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# WS2 split — searcher half: filters the raw trends, runs google_search, and
# emits RAW findings only. Separating tool-use from synthesis is the durable fix
# for the empty-turn flake: one turn no longer has to think, search, AND author
# the JSON briefing. trend_scout has no citation flow, so (unlike the creative
# producers) there is NO source-collection callback here.
understand_trends_searcher = Agent(
    # google_search + retry-wrapped (call-heavy); on worker_model (gemini-3.8-flash)
    # as the sole occupant of that global bucket, so its retries can't 429.
    model=build_gemini(config.worker_model),
    name="understand_trends_searcher",
    # A graph node: single_turn is set explicitly because a node with a parent_agent
    # otherwise defaults to "chat" mode (wait_for_output=True), which would stall the
    # graph on an empty turn. Both halves read their inputs only via `{state}` tokens,
    # so the injected predecessor output / request message is not load-bearing.
    mode="single_turn",
    include_contents="none",
    description="Conduct initial web research to briefly understand each trending topic",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.UNDERSTAND_TRENDS_SEARCHER_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=1.5,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "trend_scout",
            "subagent": "understand_trends_searcher",
        },
    ),
    tools=[google_search],
    output_key="info_gtrends_raw",
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# WS2 split — synthesizer half: tool-free / planner-free. Reads the raw findings
# (optional `{...?}` so an empty searcher turn degrades to empty synthesis and the
# wrapper retries the whole pair rather than raising KeyError inside it) and shapes
# them into the existing JSON `analyzed_trends` structure pick_trends_agent consumes.
understand_trends_synthesizer = Agent(
    # Tool-free synthesis into structured JSON; its own global flash-lite bucket.
    model=build_gemini(config.lite_planner_model),
    name="understand_trends_synthesizer",
    mode="single_turn",
    include_contents="none",
    description="Synthesizes the raw trend findings into the structured JSON briefing.",
    instruction=prompts.UNDERSTAND_TRENDS_SYNTHESIZER_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=1.5,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "trend_scout",
            "subagent": "understand_trends_synthesizer",
        },
    ),
    output_key="info_gtrends",
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)

# NOTE: the Workflow graph holds its own copies of the two agents, so mutating
# the module-level agents after this point does not affect the pipeline.
understand_trends_search_and_synthesize = Workflow(
    name="understand_trends_search_and_synthesize",
    description="Runs the raw trend search then synthesizes the JSON briefing.",
    edges=[("START", understand_trends_searcher, understand_trends_synthesizer)],
)


# Retry-on-empty: if the searcher OR synthesizer emits no final text (leaving
# `info_gtrends` unset), re-run the whole pair until populated (bounded), instead
# of crashing pick_trends_agent with `KeyError: Context variable not found`. Each
# attempt re-executes the Workflow pair under a distinct run_id. It sits in the
# orchestrator's tools as a bare node, which ADK wraps into a NodeTool running in
# the parent session (state lands there directly). Keep the `name` +
# `description` and the `PipelineRequest` input schema so the NodeTool declares
# the same tool (name, description, `request: str`) the orchestrator already
# calls under AgentTool.
understand_trends_agent_resilient = RetryUntilKeyNode(
    name="understand_trends_agent_resilient",
    # Preserve the original tool-facing description (the searcher carries it
    # verbatim).
    description=(
        "Web-research the human-picked trends (or the 5-8 most narrative-driven "
        "gathered trends) and store a structured JSON briefing."
    ),
    node=understand_trends_search_and_synthesize,
    output_key="info_gtrends",
    max_attempts=3,
    input_schema=PipelineRequest,
)


pick_trends_agent = Agent(
    # The 25->3 strategic judgment step: picker_model (a full flash) on its own
    # global bucket, isolated from the searcher/synth/root buckets.
    model=build_gemini(config.picker_model),
    name="pick_trends_agent",
    include_contents="none",
    description=(
        "Write the strategic narrative for the human-picked trends, or select the 3 "
        "trends most relevant to the campaign and write it for those."
    ),
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.PICK_TRENDS_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.4,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "trend_scout",
            "subagent": "pick_trends_agent",
        },
    ),
    output_key="selected_gtrends",
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


trend_scout = Agent(
    # Root orchestrator: mechanical tool sequencing that must call AgentTools
    # reliably. gemini-3.1-pro-preview on its own global bucket (thinking_level
    # LOW is valid on gemini-3.x — see the note below).
    model=build_gemini_with_fallback(config.critic_model, config.critic_fallback_model),
    name="trend_scout",
    retry_config=INFRA_RETRY,
    description="Determines culturally relevant Search trends to use for ad creatives.",
    # Bounded thinking: the orchestrator's job is mechanical tool sequencing, not deep
    # reasoning. On gemini-3 models, unbounded default thinking (HIGH) burned the entire
    # output budget "thinking" and hit MAX_TOKENS before emitting a tool call — but the
    # opposite extreme (thinking off) made gemini-3.5-flash emit MALFORMED_FUNCTION_CALL
    # when invoking an AgentTool with a structured argument (e.g. understand_trends_agent),
    # so the pipeline aborted right after gather_trends and never persisted anything. LOW
    # gives the model just enough room to format tool calls correctly while capping
    # thinking well short of MAX_TOKENS. NOTE: gemini-3.x deprecated the numeric
    # thinking_budget in favour of thinking_level; MINIMAL ("no thinking" for most
    # queries) reintroduces the MALFORMED_FUNCTION_CALL landmine, so LOW is the floor here.
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.LOW, include_thoughts=False
        )
    ),
    instruction=prompts.TREND_SCOUT_INSTR,
    tools=[
        AgentTool(agent=gather_trends_agent),
        understand_trends_agent_resilient,
        AgentTool(agent=pick_trends_agent),
        review_trends_tool,
        save_search_trends_to_session_state,
        save_session_state_to_gcs,
        record_research_gaps,
        write_trends_to_bq,
        write_to_file,
        memorize,
    ],
    generate_content_config=types.GenerateContentConfig(
        temperature=0.01,
        response_modalities=["TEXT"],
        labels={
            "agentic_wf": "trend_scout",
            "agent": "trend_scout",
            "subagent": "root_agent",
        },
    ),
    before_agent_callback=[
        callbacks.load_session_state,
    ],
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
    after_agent_callback=callbacks.log_final_state_summary,
)

# Set as root agent
root_agent = trend_scout

# Wrap in an App with resumability enabled — required for the opt-in
# `review_trends` LongRunningFunctionTool to pause and resume across separate
# /runs calls. `root_agent` stays exported unchanged; the resumable App is what
# both the runserver runner and deployment/deploy_agent.py (via AdkApp) use.
# `plugins` is the opt-in Model Armor screen (empty unless MODEL_ARMOR_TEMPLATE
# is set), scoped to the root's own turns — see agent_common/safety.py.
app = App(
    name="trend_scout",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
    plugins=build_safety_plugins(root_agent_names={root_agent.name}),
)
