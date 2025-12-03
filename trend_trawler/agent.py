import logging
import warnings

from google.genai import types
from google.adk.agents import Agent
from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools import google_search

from .tools import (
    save_search_trends_to_session_state,
    save_session_state_to_gcs,
    write_trends_to_bq,
    get_daily_gtrends,
    write_to_file,
    memorize,
)
from . import callbacks
from .config import config


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


# --- TREND SUBAGENTS ---
gather_trends_agent = Agent(
    model=config.worker_model,
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
    generate_content_config=types.GenerateContentConfig(
        temperature=1.0,
        response_modalities=["TEXT"],
        labels={
            "agentic_wf": "trend_trawler",
            "agent": "trend_trawler",
            "subagent": "gather_trends_agent",
        },
    ),
    # output_key="start_gtrends",
    before_model_callback=callbacks.rate_limit_callback,
)


understand_trends_agent = Agent(
    model=config.worker_model,
    name="understand_trends_agent",
    include_contents="none",
    description="Conduct initial web research to briefly understand each trending topic",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""
    You are a Cultural Trend Researcher. 
    Your goal is to prepare a structured briefing for a creative strategist. You want to find topics that possess cultural, social, or entertainment value.

    <CONTEXT>
        <raw_gtrends>
        {raw_gtrends}
        </raw_gtrends>
    </CONTEXT>

    ### Instructions
    1. **Filter:** Review the list in <raw_gtrends>. Select the top 5-8 terms that appear to be narrative-driven stories (news, memes, celebrity, sports, entertainment). Ignore searches about specific sporting events.
    2. **Research:** Use the `google_search` tool to investigate *only* these selected terms. 
    3. **Synthesize:** Create a JSON output summarizing the cultural context of these terms.

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
            "agentic_wf": "trend_trawler",
            "agent": "trend_trawler",
            "subagent": "understand_trends_agent",
        },
    ),
    tools=[google_search],
    output_key="info_gtrends",
    before_model_callback=callbacks.rate_limit_callback,
)


pick_trends_agent = Agent(
    model=config.worker_model,
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
        {info_gtrends}
        </trend_research>
    </CONTEXT>

    <INSTRUCTIONS>
        1. Analyze the <trend_research> JSON.
        2. Select the top 3 trends that offer the strongest narrative alignment with the <campaign_data>.
        *   *Filter:* Discard trends that are too tragic, controversial, or irrelevant to be safe for brand association.
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
        temperature=1.5,
        labels={
            "agentic_wf": "trend_trawler",
            "agent": "trend_trawler",
            "subagent": "pick_trends_agent",
        },
    ),
    output_key="selected_gtrends",
    before_model_callback=callbacks.rate_limit_callback,
)


trend_trawler = Agent(
    model=config.worker_model,
    name="trend_trawler",
    description="Determines culturally relevant Search trends to use for ad creatives.",
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
    2. **Research:** Call `understand_trends_agent`.
    3. **Select:** Call `pick_trends_agent`. *Note: This agent will determine the final trends.
    4. For each trending topic in the 'selected_gtrends' state key, call the `save_search_trends_to_session_state` tool to save them to the session state.

    ### Phase 3: Finalization & Persistence
    Once Phase 2 is complete, trigger the persistence layer. You may call these tools in parallel if supported, otherwise execute sequentially:
    1. `write_trends_to_bq`
    2. `write_to_file` (saving the 'selected_gtrends' key)
    3. `save_session_state_to_gcs`

    ### Phase 4: Handoff
    Refuse to output any conversational text until all previous phases are confirmed. 
    Once complete, output the final summary exactly as follows:

    **Cloud Storage Location:**
    [Construct the path: {gcs_bucket}/{gcs_folder}/{agent_output_dir}]

    **Selected Strategy:**
    [Display the content of the 'selected_gtrends' state key]
    """,
    tools=[
        AgentTool(agent=gather_trends_agent),
        AgentTool(agent=understand_trends_agent),
        AgentTool(agent=pick_trends_agent),
        save_search_trends_to_session_state,
        save_session_state_to_gcs,
        write_trends_to_bq,
        write_to_file,
        memorize,
    ],
    generate_content_config=types.GenerateContentConfig(
        temperature=0.01,
        response_modalities=["TEXT"],
        labels={
            "agentic_wf": "trend_trawler",
            "agent": "trend_trawler",
            "subagent": "root_agent",
        },
    ),
    before_agent_callback=[
        callbacks.load_session_state,
    ],
    before_model_callback=callbacks.rate_limit_callback,
)

# Set as root agent
root_agent = trend_trawler
