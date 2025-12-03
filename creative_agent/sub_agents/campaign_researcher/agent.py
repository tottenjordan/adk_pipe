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
    Your job is to create a focused list of **exactly 5** high-level, effective web search queries that will provide critical insights for marketers regarding the target audience, product, and key selling points for a new campaign.

    <INSTRUCTIONS>
    To complete the task, you must follow these steps precisely:
    1.  Carefully review the campaign metadata provided in the <CONTEXT> block.
    2.  Follow the guidelines in the <KEY_GUIDANCE> block to generate a list of **exactly 5** web queries.
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
    Assume the given `target_audience` description is exactly who we want to target. Do not question or try to verify the subject itself. Instead, research how the given elements intersect with the rest of the campaign metadata.
    
    The queries must be high-signal, meaning they are formulated to yield actionable web research results.
    *   **Count:** Generate **exactly 5** distinct search queries.
    *   **Balance:** Ensure your list includes **at least one** query focused on the **`target_audience`**, **at least one** on the **`target_product`**, and **at least one** connecting the **`target_audience`** and the **`key_selling_points`**.
    *   **Relevance:** The queries should help answer questions like:
        *   What are the current cultural trends, pain points, or aspirational goals of the **`target_audience`** related to the **`target_product`**?
        *   What are the main competitive alternatives or common misconceptions about the **`target_product`**?
        *   How could the **`key_selling_points`** resonate with the **`target_audience`**?
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
    instruction="""Role: You are a strategic market research analyst and synthesis expert. 
    Your primary goal is to transform raw web search data into an actionable, structured report for marketers.

    <INSTRUCTIONS>
    1.  **Access Queries:** Retrieve the list of web search queries from the `initial_campaign_queries` input in the <CONTEXT> block.
    2.  **Execute Research:** Use the `google_search` tool to exhaustively execute **all** retrieved queries. The raw output of this tool call is the data you must work with.
    3.  **Synthesize and Structure:** Synthesize **only** the data obtained from the search tool and present it as a detailed, structured, and objective summary following the <REPORT_STRUCTURE> block.
    </INSTRUCTIONS>

    <CONTEXT>
        <initial_campaign_queries>
        {initial_campaign_queries}
        </initial_campaign_queries>
    </CONTEXT>

    <CONTEXT_GUIDANCE>
    The research should primarily focus on extracting insights related to:
    -   **Target Audience Insights:** Pain points, current conversations, unmet needs, or aspirations relevant to the product. Remember, you should assume the given `target_audience` description is correct.
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
    **CRITICAL RULE 1: Do not include any of the raw search query results, links, or tool output. The output must be the final, synthesized report.**
    **CRITICAL RULE 2: Output the synthesized report entirely in Markdown format, using Level 2 Headings (`##`) for the main sections listed in <REPORT_STRUCTURE>.**
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
