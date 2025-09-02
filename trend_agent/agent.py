import logging

logging.basicConfig(level=logging.INFO)

from google.genai import types

from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.agents import Agent, SequentialAgent
from google.adk.tools import google_search, load_artifacts

from .tools import get_daily_gtrends, write_to_file, save_search_trends_to_session_state
from .shared_libraries import callbacks
from .shared_libraries.config import config


# --- TREND SUBAGENTS ---
gather_trends_agent = Agent(
    model=config.worker_model,
    name="gather_trends_agent",
    description="Get top 25 trending terms from Google Search.",
    instruction="""
    Role: You are a highly accurate AI assistant specialized in factual retrieval using available tools. 

    1. Use the `get_daily_gtrends` tool to gather the latest trends from Google Search.
      - This tool produces a formatted markdown table of the trends, which can be found in the 'markdown_table' key of the tool's response.
    2. Generate a numbered list of all trending topics.

    Output *only* the numbered list of search terms.
    """,
    tools=[get_daily_gtrends],
    generate_content_config=types.GenerateContentConfig(
        temperature=1.0,
        # response_modalities=["TEXT"],
    ),
    output_key="start_gtrends",
    before_model_callback=callbacks.rate_limit_callback,
)


understand_trends_agent = Agent(
    model=config.worker_model,
    name="understand_trends_agent",
    description="Conduct initial web research to briefly understand each trending topic",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""
    You are a diligent and exhaustive researcher. Your task is to conduct initial web research for each trending Search term.

    1. Review the trending search terms provided in the 'start_gtrends' state key.
    2. Use the 'google_search' tool to briefly understand each term and why it's trending.
    3. Synthesize the results into a detailed summary that follows the **Important Guidelines** listed below.


    ### Important Guidelines
    1. Your output should list each trending search term from the 'start_gtrends' state key
    2. For each trending search term, **provide the following bullets:**
        - Briefly what the term represents.
        - Briefly why the term is likely trending.

    Output *only* the structured list of search terms.
    """,
    generate_content_config=types.GenerateContentConfig(
        temperature=1.5,
    ),
    tools=[google_search],
    output_key="info_gtrends",
    before_model_callback=callbacks.rate_limit_callback,
)


pick_trends_agent = Agent(
    model=config.worker_model,
    name="pick_trends_agent",
    description="Determine subset of Search trends most culturally relevant to the target audience.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""
    You are a marketing campaign strategist. Your goal is to find the most culturally relevant trends for the given campaign.
    Review the campaign metadata provided in the **Input Data**, then complete the **Instructions**.

    ---
    ### Input Data

    <brand>{brand}</brand>

    <target_product>{target_product}</target_product>
    
    <key_selling_points>
    {key_selling_points}
    </key_selling_points>

    <target_audience>
    {target_audience}
    </target_audience>

    ---
    ### Instructions
    1. Review the trending search terms provided in the 'info_gtrends' state key and select the best 3-5 trends based on:
        - Cultural relevance with the <target_audience/>.
        - Opportunity to market the <target_product/> and <key_selling_points/>.
    2. Provide detailed rationale for your selections, explaining why these specific trends are most relevant.
    3. For each selected search term, **provide the following:**
        - Brief overview of the search term and why it's trending.
        - Detailed rationale for target audience appeal.
        - Any themes of the trend that can be used to market the <target_product/>.

    Use the `google_search` tool to support your decisions.

    **CRITICAL RULE:** The trending topics you select, should only contain topics found in the 'info_gtrends' state key. 

    """,
    generate_content_config=types.GenerateContentConfig(
        temperature=1.5,
    ),
    tools=[google_search],
    output_key="selected_gtrends",
    before_model_callback=callbacks.rate_limit_callback,
)


# # Sequential agent for gathering relevant trends
# trend_spotter_pipeline = SequentialAgent(
#     name="trend_spotter_pipeline",
#     description="This is a sequential agent that executes the sub agents in the provided order.",
#     sub_agents=[
#         gather_trends_agent,
#         understand_trends_agent,
#         pick_trends_agent,
#     ],
# )


root_agent = Agent(
    model=config.worker_model,
    name="root_agent",
    description="Determines culturally relevant Search trends to use for ad creatives.",
    instruction="""You are an Expert AI Marketing Research & Strategy Assistant.

    Once initiated, immediately begin the steps below.

    1. Call `gather_trends_agent` as a tool to gather the latest Google Search Trends. 
    2. Call `understand_trends_agent` as a tool to conduct web research about each trending topic.
    3. Call `pick_trends_agent` as a tool to determine the most relevant subset of trends for this campaign. 
    4. For each trending topic in the 'selected_gtrends' state key, call the `save_search_trends_to_session_state` tool to save them to the session state.

    Once these three tasks are complete, complete the following actions:

    Action 1: Save to File
    Call the `write_to_file` tool to save the markdown content in the 'selected_gtrends' state key.

    Action 2: Display Selected Trends to User      
    Display the selected trends and insights in the 'selected_gtrends' state key to the user.

    """,
    tools=[
        # AgentTool(agent=trend_spotter_pipeline),
        AgentTool(agent=gather_trends_agent),
        AgentTool(agent=understand_trends_agent),
        AgentTool(agent=pick_trends_agent),
        write_to_file,
        save_search_trends_to_session_state,
    ],
    generate_content_config=types.GenerateContentConfig(
        temperature=0.01,
        response_modalities=["TEXT"],
    ),
    before_agent_callback=[
        callbacks._load_session_state,
    ],
    before_model_callback=callbacks.rate_limit_callback,
)
