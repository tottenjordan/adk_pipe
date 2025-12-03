import logging
import warnings
from pydantic import BaseModel, Field

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


# --- SCHEMA DEFINITIONS ---
# TODO: consolidate with schema class used in agent.py

class TrendSearchQuery(BaseModel):
    """Model representing a specific search query for web search."""

    search_query: str = Field(
        description="A highly specific and targeted query for web search."
    )


class TrendQueryList(BaseModel):
    """Model for providing evaluation feedback on research quality."""

    queries: list[TrendSearchQuery] | None = Field(
        default=None,
        description="A list of specific, targeted web queries to provide initial insights for marketers.",
    )


# --- AGENT DEFINITIONS ---
gs_web_planner = Agent(
    model=config.lite_planner_model,
    name="gs_web_planner",
    include_contents="none",
    description="Generates initial queries to understand why the 'target_search_trends' are trending.",
    instruction="""Role: You are an expert cultural strategist and trend analyst. 
    Your job is to create a focused list of **exactly 5** high-signal web search queries that will help marketers understand the cultural significance and context of a trending Google Search topic.

    <INSTRUCTIONS>
    To complete the task, you must follow these steps precisely:
    1.  Review the trending topic and target audience provided in the <CONTEXT> block.
    2.  Follow the guidelines in the <KEY_GUIDANCE> block to generate a list of **exactly 5** highly effective search queries.
    3.  **CRITICAL FOR COMPLETION:** Ensure your output is a single, valid JSON object containing the generated queries.
    </INSTRUCTIONS>

    <CONTEXT>
        <target_search_trends>
        {target_search_trends}
        </target_search_trends>

        <target_audience>
        {target_audience}
        </target_audience>
    </CONTEXT>

    <KEY_GUIDANCE>
    The queries must be high-signal, meaning they are formulated to yield actionable insights for campaign development.

    *   **Count:** Generate **exactly 5** distinct search queries.
    *   **Balance:** Your queries must cover **three primary areas**:
        1.  **Trend Context:** Why is the trend happening now? (e.g., recent event, new product, cultural shift)
        2.  **Trend Entities & Narrative:** Who are the key players (people, brands, movements) involved, and what is the underlying narrative or conflict?
        3.  **Audience Connection:** How does the trend intersect with the interests, language, or values of the **`target_audience`**?
    *   **Format:** Queries should be optimized for a modern web search engine (i.e., not long, conversational sentences). Use specific keywords and quotation marks for precision.
    </KEY_GUIDANCE>

    ---
    ### Output Format
    **STRICT RULE: Your entire output MUST be a valid JSON object matching the 'TrendQueryList' schema. Do not include any introductory text, reasoning, or markdown outside the JSON block.**
    """,
    output_schema=TrendQueryList,
    output_key="initial_gs_queries",
)


gs_web_searcher = Agent(
    model=config.worker_model,
    name="gs_web_searcher",
    description="Performs the crucial first pass of web research about the trending Search terms.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""Role: You are a cultural trend analyst and synthesis expert. Your primary goal is to transform raw web search data about a trending topic into an urgent, actionable, and culturally relevant summary for marketers.

    <INSTRUCTIONS>
    1.  **Access Queries:** Retrieve the list of web search queries from the `initial_gs_queries` input in the <CONTEXT> block.
    2.  **Execute Research:** Use the `google_search` tool to exhaustively execute **all** retrieved queries. The raw output of this tool call is the data you must synthesize.
    3.  **Synthesize and Structure:** Synthesize **only** the data obtained from the search tool and present it as a detailed, structured, and objective trend report following the <REPORT_STRUCTURE> block.
    </INSTRUCTIONS>

    <CONTEXT>
        <initial_gs_queries>
        {initial_gs_queries}
        </initial_gs_queries>
    </CONTEXT>
    
    <CONTEXT_GUIDANCE>
    The research synthesis must focus on providing marketers with timely, strategic insight. Specifically, prioritize:
    -   **Immediacy and Trajectory:** What is the current status of the trend? Is it peaking, or is it gaining momentum?
    -   **Cultural Narrative:** What is the central story, sentiment (positive/negative), or public conversation surrounding the trend? What are the key quotes or memes?
    -   **Audience Resonance:** How does the trend connect to the target audience's values, language, or social platforms? What is the *potential* for this trend to intersect with the campaign?
    </CONTEXT_GUIDANCE>

    
    <REPORT_STRUCTURE>
    Your output must be a single, detailed, easy-to-read report sectioned with bold headings. The report should contain the following specific sections:

    1.  **Trend Overview & Trajectory:** (Briefly define the trend, its current status, and an estimate of its immediate lifespan or staying power.)
    2.  **Key Entities and Cultural Narrative:** (Identify the core people/brands/events driving the trend and summarize the public sentiment or underlying cultural story.)
    3.  **Marketing Opportunity Analysis:** (**CRITICAL:** Identify 2-3 specific, actionable ways the trend could be leveraged to create culturally relevant messaging for the campaign, specifically considering the target audience.)
    </REPORT_STRUCTURE>

    
    ---
    ### Final Instruction
    **CRITICAL RULE 1: Do not include any of the raw search query results, links, or tool output. The output must be the final, synthesized report.**
    **CRITICAL RULE 2: Output the synthesized report entirely in Markdown format, using Level 2 Headings (`##`) for the main sections listed in <REPORT_STRUCTURE>.**
    """,
    tools=[google_search],
    output_key="gs_web_search_insights",
    after_agent_callback=callbacks.collect_research_sources_callback,
)

    # 4.  **Risk Assessment:** (Identify any potential pitfalls, controversies, or negative associations linked to the trend that marketers must be aware of.)

gs_sequential_planner = SequentialAgent(
    name="gs_sequential_planner",
    description="Executes sequential research tasks for trends in Google Search.",
    sub_agents=[gs_web_planner, gs_web_searcher],
)
