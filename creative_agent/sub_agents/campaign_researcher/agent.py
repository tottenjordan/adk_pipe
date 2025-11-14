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

class CampaignSearchQuery(BaseModel):
    """Model representing a specific search query for web search."""

    search_query: str = Field(
        description="A highly specific and targeted query for web search."
    )


class CampaignQueryList(BaseModel):
    """Model for providing evaluation feedback on research quality."""

    queries: list[CampaignSearchQuery] | None = Field(
        default=None,
        description="A list of specific, targeted web queries to provide initial insights for marketers.",
    )


# --- AGENT DEFINITIONS ---
campaign_web_planner = Agent(
    model=config.lite_planner_model,
    name="campaign_web_planner",
    include_contents="none",
    description="Generates initial queries to guide web research about concepts described in the campaign metadata.",
    instruction="""Role: You are an expert market research strategist and query optimization specialist.
    Your job is to create a focused list of high-level, effective web search queries (4-6 total) that will provide critical insights for marketers regarding the target audience, product, and key selling points for a new campaign.

    <INSTRUCTIONS>
    To complete the task, you must follow these steps precisely:
    1.  Carefully review the campaign metadata provided in the <CONTEXT> block.
    2.  Follow the guidelines in the <KEY_GUIDANCE> block to generate a list of 4-6 web queries.
    3.  **CRITICAL FOR COMPLETION:** Ensure your output is a single, valid JSON object containing the generated queries.
    </INSTRUCTIONS>

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

    <KEY_GUIDANCE>
    The queries must be high-signal, meaning they are formulated to yield actionable web research results.
    *   **Balance:** Ensure your list includes **at least one** query focused on the **target audience**, **at least one** on the **target product**, and **at least one** connecting the audience and the **key selling points**.
    *   **Relevance:** The queries should help answer questions like:
        *   What are the current cultural trends, pain points, or aspirational goals of the `target_audience` related to the {target_product}?
        *   What are the main competitive alternatives or common misconceptions about the {target_product}?
        *   How could the `key_selling_points` resonate with the `target_audience`?
    *   **Format:** Queries should be optimized for a modern web search engine (i.e., not long, conversational sentences). Use quotation marks around specific phrases or product names where appropriate.
    </KEY_GUIDANCE>

    ---
    ### Output Format
    **STRICT RULE: Your entire output MUST be a valid JSON object matching the 'CampaignQueryList' schema. Do not include any introductory text, reasoning, or markdown outside the JSON block.**
    """,
    output_schema=CampaignQueryList,
    output_key="initial_campaign_queries",
)


campaign_web_searcher = Agent(
    model=config.worker_model,
    name="campaign_web_searcher",
    description="Performs the crucial first pass of web research about the campaign guide.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""Role: You are a strategic market research analyst and synthesis expert. Your primary goal is to transform raw web search data into an actionable, structured report for marketers.

    <INSTRUCTIONS>
    1. **Access Queries:** Retrieve the list of web search queries from the `initial_campaign_queries` state key.
    2. **Execute Research:** Use the `google_search` tool to exhaustively execute **all** retrieved queries.
    3. **Synthesize and Structure:** Synthesize the search results and present them in a detailed, structured, and objective summary following the <REPORT_STRUCTURE> block.
    </INSTRUCTIONS>

    <CONTEXT_GUIDANCE>
    The research should primarily focus on:
    -   **Target Audience Insights:** Pain points, current conversations, unmet needs, or aspirations relevant to the product.
    -   **Product/Market Landscape:** Competitive alternatives, common use cases, and general market sentiment around the product category.
    -   **Key Selling Point Validation:** Evidence, data, or public opinion that supports or contradicts the effectiveness of the intended key selling points.
    -   **Cultural Relevance:** Current trends or cultural shifts that could impact campaign messaging.
    </CONTEXT_GUIDANCE>

    <REPORT_STRUCTURE>
    Your output must be a single, detailed, easy-to-read report sectioned with bold headings. The report should contain the following specific sections:

    1.  **Target Audience and Behavioral Insights:** (Summarize findings about the audience's needs, language, and online behavior.)
    2.  **Product Landscape and Competitive Context:** (Detail the market position, identify 2-3 main alternatives, and note any common misconceptions.)
    3.  **Strategic Opportunities & Key Message Validation:** (Highlight 2-3 most compelling, research-backed insights that validate or refine the campaign's key selling points.)
    4.  **Key Research Gaps/Next Steps:** (Briefly note any critical information that could not be found or requires further investigation.)
    </REPORT_STRUCTURE>

    ---
    ### Final Instruction
    **CRITICAL RULE: Do not include any of the raw search query results, links, or tool output. The output must be the final, synthesized, and structured report.**
    """,
    tools=[google_search],
    output_key="campaign_web_search_insights",
    after_agent_callback=callbacks.collect_research_sources_callback,
)


    # You are a diligent and exhaustive researcher. Your task is to conduct initial web research for concepts described in the campaign guide.
    # You will be provided with a list of web queries in the 'initial_campaign_queries' state key.
    # Use the 'google_search' tool to execute all queries. 
    # Synthesize the results into a detailed summary.


ca_sequential_planner = SequentialAgent(
    name="ca_sequential_planner",
    description="Executes sequential research tasks for concepts described in the campaign guide.",
    sub_agents=[campaign_web_planner, campaign_web_searcher],
)
