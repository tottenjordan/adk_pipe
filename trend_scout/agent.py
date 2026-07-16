import logging
import warnings

from google.genai import types
from google.adk.agents import Agent, SequentialAgent
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
from agent_common import build_gemini, RetryUntilKeyAgent
from . import callbacks
from .config import config, INFRA_RETRY


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


# --- TREND SUBAGENTS ---
gather_trends_agent = Agent(
    model=build_gemini(config.worker_model),
    name="gather_trends_agent",
    include_contents="none",
    description="Get top 25 trending terms from Google Search.",
    instruction="""
    Role: You are a data pipeline controller. 

    1. Call `get_daily_gtrends` to retrieve the latest trends.
    2. The tool will automatically save the raw list to the session state.

    Output a confirmation message containing the count of trends retrieved. Do NOT list them.
    """,
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
    model=build_gemini(config.worker_model),
    name="understand_trends_searcher",
    include_contents="none",
    description="Conduct initial web research to briefly understand each trending topic",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""
    You are a Cultural Trend Researcher gathering raw material for a creative strategist. You want topics that possess cultural, social, or entertainment value.

    <CONTEXT>
        <raw_gtrends>
        {raw_gtrends?}
        </raw_gtrends>
    </CONTEXT>

    ### Instructions
    0. If <raw_gtrends> is empty, the upstream trend gather did not run. Do NOT invent terms — report that no trends were available and stop.
    1. **Filter:** Review the list in <raw_gtrends>. Select the top 5-8 terms that appear to be narrative-driven stories (news, memes, celebrity, sports, entertainment). Ignore searches about specific sporting events.
    2. **Research:** Use the `google_search` tool to investigate *only* these selected terms.
    3. **Report RAW Findings:** For each selected term, list the concrete facts, entities, dates, and the cultural/social angle you found. Do NOT format as final JSON and do NOT omit specifics — the next agent needs the raw material to structure. Plain text grouped by term is fine.
    """,
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
    model=build_gemini(config.worker_model),
    name="understand_trends_synthesizer",
    include_contents="none",
    description="Synthesizes the raw trend findings into the structured JSON briefing.",
    instruction="""
    You are a Cultural Trend Researcher. Turn the raw findings below into a structured briefing for a creative strategist.

    <CONTEXT>
        <info_gtrends_raw>
        {info_gtrends_raw?}
        </info_gtrends_raw>
    </CONTEXT>

    ### Instructions
    Synthesize **only** the data in <info_gtrends_raw> into a JSON object summarizing the cultural context of each term.

    ### Output Format
    Output *only* a valid JSON object with the list of analyzed trends. Do not output markdown.
    Structure:
    {
      "analyzed_trends": [
        {
          "term": "Search Term",
          "category": "Broad Category (e.g., Sports, Pop Culture, Politics)",
          "context": "Brief explanation of what happened.",
          "cultural_angle": "Why this matters to culture/society right now (e.g., 'Sparking debate on AI', 'Nostalgia for 90s')."
        }
      ]
    }
    """,
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
    model=build_gemini(config.worker_model),
    name="pick_trends_agent",
    include_contents="none",
    description="Determine subset of Search trends most culturally relevant to the target audience.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""
    You are a Lead Creative Strategist. 
    Your goal is to identify the "Strategic Bridge" between current cultural trends and a specific brand campaign.

    <CONTEXT>
        <campaign_data>
            Brand: {brand}
            Product: {target_product}
            Key Selling Point(s): {key_selling_points}
            Target Audience: {target_audience}
        </campaign_data>

        <trend_research>
        {info_gtrends?}
        </trend_research>
    </CONTEXT>

    <INSTRUCTIONS>
        0. If <trend_research> is empty, the upstream trend research did not run.
           Do NOT invent trends — output a single line noting that no trend
           research was available, and stop.
        1. Analyze the <trend_research> JSON.
        2. Select exactly 3 trends that offer the strongest narrative alignment
           with the <campaign_data>. You MUST return 3. Only return fewer if
           <trend_research> contains fewer than 3 distinct trends, in which case
           return every trend available.
        3. For each selected trend, define the "Strategic Bridge"—the specific angle that connects the trend's cultural mood to the product's unique selling points.

        Output your findings in the requested format.
    </INSTRUCTIONS>

    <OUTPUT_FORMAT>
        ## Selected Trends Strategy

        ### [trending search term]
        * **The "Hook":** [One distinct, punchy headline summarizing the marketing angle]
        * **Context:** [1 sentence on what the trend is, based on provided research]
        * **Why it fits:** [Explain why the `target_audience` cares about this]
        * **The Strategic Bridge:** [CRITICAL: Explain exactly how to position the {target_product} within this trend. How should the Key Selling Point(s) be highlighted to match the trend's vibe?]
    </OUTPUT_FORMAT>

    **Constraint:** Do not repeat campaign metadata. Focus 100% on the analysis.
    """,
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
    model=build_gemini(config.worker_model),
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
    instruction="""You are the Lead Campaign Orchestrator.
    Your goal is to manage the end-to-end execution of the Trend Research Pipeline.

    ### Phase 1: Initialization
    1. **Check & Store:** Verify if the following variables are present. If present, immediately call the `memorize` tool for ALL of them in a single turn (or as parallel calls).
    - `brand`
    - `target_audience`
    - `target_product`
    - `key_selling_points`

    ### Phase 2: Execution Pipeline
    Execute the following agents in strict sequence. Do not proceed to the next until the current tool reports success.

    1. **Gather:** Call `gather_trends_agent`.
    2. **Research:** Call `understand_trends_agent_resilient`.
    3. **Select:** Call `pick_trends_agent`. *Note: This agent will determine the final trends.
    4. For each trending topic in the 'selected_gtrends' state key, call the `save_search_trends_to_session_state` tool to save them to the session state.

    ### Phase 3: Finalization & Persistence
    Once Phase 2 is complete, trigger the persistence layer. Call `record_research_gaps` FIRST so the note is captured before the session state is snapshotted; the remaining tools may run in parallel if supported, otherwise execute sequentially:
    1. `record_research_gaps` (records any upstream research-degradation notes)
    2. `write_trends_to_bq`
    3. `write_to_file` (saving the 'selected_gtrends' key)
    4. `save_session_state_to_gcs`

    ### Phase 4: Handoff
    Refuse to output any conversational text until all previous phases are confirmed.
    Once complete, output the final summary exactly as follows:

    **Cloud Storage Location:**
    [Construct the path: {gcs_bucket}/{gcs_folder}/{agent_output_dir}]

    **Selected Strategy:**
    [Display the content of the 'selected_gtrends' state key]

    **Research Notes:** {research_gaps?}
    [Only include this line if research_gaps is non-empty; otherwise omit it entirely.]
    """,
    tools=[
        AgentTool(agent=gather_trends_agent),
        AgentTool(agent=understand_trends_agent_resilient),
        AgentTool(agent=pick_trends_agent),
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
