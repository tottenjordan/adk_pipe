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


gs_web_planner = Agent(
    model=config.lite_planner_model,
    name="gs_web_planner",
    include_contents="none",
    description="Generates initial queries to understand why the 'target_search_trends' are trending.",
    instruction="""You are a research strategist. 
    Your job is to create high-level queries that will help marketers better understand the cultural significance of Google Search trends.

    Review the search trend and target audience provided in the <CONTEXT> block, then proceed to the <INSTRUCTIONS> to complete your task.

    ---
    <CONTEXT>
        <target_search_trends>
        {target_search_trends}
        </target_search_trends>

        <target_audience>
        {target_audience}
        </target_audience>
    </CONTEXT>

    ---
    <INSTRUCTIONS>
    1. Generate 4-5 queries that will provide more context for the target search trend: <target_search_trends> 
    2. Your questions should help answer questions like:
        - Why are these search terms trending? Who is involved?
        - Describe any key entities involved (i.e., people, places, organizations, named events, etc.), and the relationships between these key entities, especially in the context of the trending topic, or if possible the <target_audience>.
        - Explain the cultural significance of the trend.
        - Are there any related themes that would resonate with the <target_audience>?
    </INSTRUCTIONS>

    <RECAP>
    **CRITICAL RULE:** Your output should just include a numbered list of queries. Nothing else.
    </RECAP>
    """,
    output_key="initial_gs_queries",
)


gs_web_searcher = Agent(
    model=config.worker_model,
    name="gs_web_searcher",
    description="Performs the crucial first pass of web research about the trending Search terms.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""
    You are a diligent and exhaustive researcher. 
    Your task is to conduct initial web research for the trending Search terms.
    Use the 'google_search' tool to execute all queries listed in 'initial_gs_queries'. 
    Synthesize the results into a detailed summary.
    """,
    tools=[google_search],
    output_key="gs_web_search_insights",
    after_agent_callback=callbacks.collect_research_sources_callback,
)


gs_sequential_planner = SequentialAgent(
    name="gs_sequential_planner",
    description="Executes sequential research tasks for trends in Google Search.",
    sub_agents=[gs_web_planner, gs_web_searcher],
)
