import logging
import warnings
from google.genai import types
from google.adk.tools import google_search
from google.adk.planners import BuiltInPlanner
from google.adk.agents import Agent, SequentialAgent

from ...config import config
from ... import callbacks


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


campaign_web_planner = Agent(
    model=config.lite_planner_model,
    name="campaign_web_planner",
    include_contents="none",
    description="Generates initial queries to guide web research about concepts described in the campaign metadata.",
    instruction="""You are a research strategist. 
    Your job is to create high-level queries that will help marketers better understand the target audience, target product, and key selling points for a campaign.

    <INSTRUCTIONS>
    To complete the task, you need to follow these steps:
    1. Review the campaign metadata provided in the <CONTEXT> block
    2. Follow the tips provided in the <KEY_GUIDANCE> block to generate a list of 4-6 web queries that will help you better understand each concept from the campaign metadata.
    </INSTRUCTIONS>

    ---
    <CONTEXT>
        <target_audience>
        {target_audience}
        </target_audience>

        <target_product>
        {target_product}
        </target_product>
        
        <key_selling_points>
        {key_selling_points}
        </key_selling_points>
    </CONTEXT>
    
    ---
    <KEY_GUIDANCE>
    The queries should help answer questions like:
    *  What's relevant, distinctive, or helpful about the {target_product}?
    *  What are some key attributes about the target audience?
    *  Which key selling points would the target audience best resonate with? Why? 
    *  How could marketers make a culturally relevant advertisement related to product insights?
    </KEY_GUIDANCE>
    
    ---
    ### Final Instructions
    Make sure your web queries address the points made in the <KEY_GUIDANCE>.
    **CRITICAL RULE: Your output should just include a numbered list of queries. Nothing else.**
    """,
    output_key="initial_campaign_queries",
)


campaign_web_searcher = Agent(
    model=config.worker_model,
    name="campaign_web_searcher",
    description="Performs the crucial first pass of web research about the campaign guide.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""
    You are a diligent and exhaustive researcher. Your task is to conduct initial web research for concepts described in the campaign guide.
    You will be provided with a list of web queries in the 'initial_campaign_queries' state key.
    Use the 'google_search' tool to execute all queries. 
    Synthesize the results into a detailed summary.
    """,
    tools=[google_search],
    output_key="campaign_web_search_insights",
    after_agent_callback=callbacks.collect_research_sources_callback,
)

ca_sequential_planner = SequentialAgent(
    name="ca_sequential_planner",
    description="Executes sequential research tasks for concepts described in the campaign guide.",
    sub_agents=[campaign_web_planner, campaign_web_searcher],
)
