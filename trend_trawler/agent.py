import os
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
    description="Get top 25 trending terms from Google Search.",
    instruction="""
    Role: You are a highly accurate AI assistant specialized in factual retrieval using available tools. 

    1. Use the `get_daily_gtrends` tool to gather the latest trends from Google Search.
      - This tool produces a formatted markdown table of the trends.
    2. Generate a numbered list of all trending topics.

    Output *only* the numbered list of search terms.
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
    You are a diligent and exhaustive researcher. 
    Your task is to conduct initial web research for each trending Search term provided in the <CONTEXT> section.

    1. Review the list of trending search terms in the <start_gtrends> section of the <CONTEXT> block.
    2. For each trending search term, use the 'google_search' tool to briefly understand each search term and why it's trending.
    3. Synthesize the results into a detailed summary that follows the **Important Guidelines** listed below.

    <CONTEXT>
        <start_gtrends>
        {start_gtrends}
        </start_gtrends>
    </CONTEXT>

    ### Important Guidelines
    1. Your output should list each trending search term from the <start_gtrends> section.
    2. For each trending search term, **provide the following bullets:**
        - Briefly what the term represents.
        - Briefly why the term is likely trending.
    3.  Only list trends from the <start_gtrends> section.

    Output *only* the bulleted list of search terms.
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
    description="Determine subset of Search trends most culturally relevant to the target audience.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""
    You are a marketing campaign strategist. Your goal is to find the most culturally relevant trends for the given campaign.
    Review the campaign metadata provided in the <CONTEXT> block, then complete each task in the <INSTRUCTIONS> block.

    ---
    <CONTEXT>
        <brand>{brand}</brand>

        <target_product>{target_product}</target_product>
        
        <key_selling_points>
        {key_selling_points}
        </key_selling_points>

        <target_audience>
        {target_audience}
        </target_audience>

        <info_gtrends>
        {info_gtrends}
        </info_gtrends>
    </CONTEXT>

    ---
    <INSTRUCTIONS>
    1. Review the trending search terms provided in the <info_gtrends/> block and select the best 1-3 trends based on:
        - Cultural relevance with the <target_audience/>.
        - Opportunity to market the <target_product/> and <key_selling_points/>.
    
    2. Provide a brief rationale for your selections, explaining why these specific trends are most relevant.
    
    3. For each selected search term, **provide the following:**
        - Brief overview of the search term and why it's trending.
        - Detailed rationale for target audience appeal.
        - Any themes of the trend that can be used to market the <target_product/>.
    
    4. Format your response following the suggested structure in <OUTPUT_FORMAT>.

    <OUTPUT_FORMAT>
    ## Selected Trends
    
    [bulleted list of the selected trending search terms. Only list the search terms in this section. Save details for subsequent sections]

    ## Campaign Summary
    - <target_audience/>
    - <target_product/>
    - <key_selling_points/>

    ### [trending search term]
    - [why its trending]
    - [target audience appeal]
    - [Any themes from the trend that can be used or referenced to market the <target_product/>]
    </OUTPUT_FORMAT>

    **CRITICAL RULE:** The trending topics you select should only contain topics found in the 'info_gtrends' state key. 
    """,
    generate_content_config=types.GenerateContentConfig(
        temperature=1.5,
        labels={
            "agentic_wf": "trend_trawler",
            "agent": "trend_trawler",
            "subagent": "pick_trends_agent",
        },
    ),
    # tools=[google_search],
    output_key="selected_gtrends",
    before_model_callback=callbacks.rate_limit_callback,
)


trend_trawler = Agent(
    model=config.worker_model,
    name="trend_trawler",
    description="Determines culturally relevant Search trends to use for ad creatives.",
    instruction="""You are an Expert AI Marketing Research & Strategy Assistant.

    Once initiated, immediately begin the steps below.

    1. First, complete the following information if any is blank:
        <brand>{brand}</brand>
        <target_audience>{target_audience}</target_audience>
        <target_product>{target_product}</target_product>
        <key_selling_points>{key_selling_points}</key_selling_points>
    2. Use the `memorize` tool to store campaign metadata and search trends into the following variables:
        - `brand`, 
        - `target_audience`
        - `target_product` and
        - `key_selling_points`
        To make sure everything is stored correctly, instead of calling memorize all at once, chain the calls such that 
        you only call another `memorize` after the last call has responded. 
    3. Then, complete all steps in the <WORKFLOW/> block. Strictly follow all the steps one-by-one.

    <WORKFLOW>
    1. Call `gather_trends_agent` as a tool to gather the latest Google Search Trends. 
    2. Call `understand_trends_agent` as a tool to conduct web research about each trending topic.
    3. Call `pick_trends_agent` as a tool to determine the most relevant subset of trends for this campaign. 
    4. For each trending topic in the 'selected_gtrends' state key, call the `save_search_trends_to_session_state` tool to save them to the session state.
    5. Once the previous step is complete, call the `write_trends_to_bq` tool to write the target search trends to BigQuery.
    6. Call the `write_to_file` tool to save the markdown content in the 'selected_gtrends' state key.
    7. Call the `save_session_state_to_gcs` tool to save the session state to Cloud Storage.
    8. Once the previous steps are complete, perform the following TWO actions in sequence:

    Action 1: Display Cloud Storage location to the user
    Display the Cloud Storage URI to the user by combining the 'gcs_bucket', 'gcs_folder', and 'agent_output_dir' state keys like this: {gcs_bucket}/{gcs_folder}/{agent_output_dir}

        <EXAMPLE>
            INPUT: {gcs_bucket}/{gcs_folder}/{agent_output_dir}

            OUTPUT: gs://trend-trawler-deploy-ae/2025_09_13_19_21/trawler_output
        </EXAMPLE>

    Action 2: Display Selected Trends to User      
    Display the selected trends and insights in the 'selected_gtrends' state key to the user.
    </WORKFLOW>

    Your job is complete once all actions in the <WORKFLOW> are performed.
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
        callbacks._load_session_state,
    ],
    before_model_callback=callbacks.rate_limit_callback,
)

# Set as root agent
root_agent = trend_trawler
