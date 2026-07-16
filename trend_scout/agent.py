import logging
import warnings

from google.genai import types
from google.adk.agents import Agent, SequentialAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools import google_search

from .tools import (
    save_search_trends_to_session_state,
    save_session_state_to_gcs,
    record_research_gaps,
    write_trends_to_bq,
    get_daily_gtrends,
    write_to_file,
    memorize,
)
from .review_tools import review_trends_tool
from agent_common import build_gemini, RetryUntilKeyAgent
from . import callbacks
from . import prompts
from .config import config, INFRA_RETRY


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


# --- TREND SUBAGENTS ---
gather_trends_agent = Agent(
    # Trivial tool-output formatting; runs on its own regional gemini-2.5 bucket.
    model=build_gemini(config.gather_model, location=config.regional_model_location),
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
    # google_search + retry-wrapped (call-heavy); kept on gemini-3.5-flash but
    # now the sole occupant of that global bucket, so its retries can't 429.
    model=build_gemini(config.worker_model),
    name="understand_trends_searcher",
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

understand_trends_search_and_synthesize = SequentialAgent(
    name="understand_trends_search_and_synthesize",
    description="Runs the raw trend search then synthesizes the JSON briefing.",
    sub_agents=[understand_trends_searcher, understand_trends_synthesizer],
)


# Retry-on-empty: if the searcher OR synthesizer emits no final text (leaving
# `info_gtrends` unset), re-run the whole pair until populated (bounded), instead
# of crashing pick_trends_agent with `KeyError: Context variable not found`. The
# wrapper runs only sub_agents[0], so we wrap the SequentialAgent pair. It is
# exposed to the orchestrator as an AgentTool, so the retry runs inside AgentTool's
# isolated sub-Runner — state-delta timing across that boundary is identical to the
# top-level case the wrapper was verified against (agent_tool.py forwards each inner
# event's state_delta before our generator resumes). Keep the `name` + `description`
# so AgentTool builds the same tool declaration the orchestrator already knows.
understand_trends_agent_resilient = RetryUntilKeyAgent(
    name="understand_trends_agent_resilient",
    # Preserve the original tool-facing description so AgentTool builds the same
    # declaration the orchestrator already calls (the searcher carries it verbatim).
    description=understand_trends_searcher.description,
    sub_agents=[understand_trends_search_and_synthesize],
    output_key="info_gtrends",
    max_attempts=3,
)


pick_trends_agent = Agent(
    # The 25->3 strategic judgment step: gemini-2.5-pro on its own regional
    # bucket — both quota isolation and a quality upgrade for the pick.
    model=build_gemini(config.picker_model, location=config.regional_model_location),
    name="pick_trends_agent",
    include_contents="none",
    description="Determine subset of Search trends most culturally relevant to the target audience.",
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
    model=build_gemini(config.critic_model),
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
        AgentTool(agent=understand_trends_agent_resilient),
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
# /runs calls. `root_agent` stays exported unchanged (deployment/deploy_agent.py
# imports the bare agent); the resumable App is used by the runserver runner.
app = App(
    name="trend_scout",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
