import logging
import warnings
from pydantic import BaseModel, Field

from google.genai import types
from google.adk.tools import google_search
from google.adk.planners import BuiltInPlanner
from google.adk.agents import Agent, SequentialAgent

from agent_common import build_gemini, RetryUntilKeyAgent
from ...config import config
from ...schemas import SearchQuery
from ... import callbacks


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


# DoE arm seam (2026-07-17): the campaign half's (planner, worker, location) come
# from config.campaign_models(), driven by CAMPAIGN_RESEARCH_PLACEMENT. Default
# `regional_25` = the shipped #101 spread (gemini-2.5 @ us-central1); `global_3x`
# and `global_altbucket` are the DoE treatment arms. Resolved once at import.
_CA_LITE, _CA_WORKER, _CA_LOC = config.campaign_models()


# --- SCHEMA DEFINITIONS ---
# SearchQuery is the shared single-query model from creative_agent.schemas.
class CampaignQueryList(BaseModel):
    """Model for providing evaluation feedback on research quality."""

    queries: list[SearchQuery] | None = Field(
        default=None,
        description="A list of specific, targeted web queries to provide initial insights for marketers.",
    )


# --- AGENT DEFINITIONS ---
campaign_web_planner = Agent(
    # Quota spread (#94/#101): campaign half runs on a separate bucket so it doesn't
    # double up on the trend planner's bucket. Model+location come from the DoE arm
    # (config.campaign_models()); default arm = regional gemini-2.5 @ us-central1.
    model=build_gemini(_CA_LITE, location=_CA_LOC),
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
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# WS2 split — searcher half: runs google_search and emits RAW findings only.
# Separating tool-use from synthesis is the durable fix for the empty-turn flake:
# no single turn has to think, search, AND author a long report. Grounding
# metadata lives on this turn, so `collect_research_sources_callback` stays here.
campaign_web_searcher = Agent(
    # Quota spread (#94/#101): campaign worker bucket from the DoE arm. google_search
    # grounding is verified on each arm's model @ its location (default:
    # gemini-2.5-flash @ us-central1; Arm C: gemini-3-flash-preview @ global — Task 0a).
    model=build_gemini(_CA_WORKER, location=_CA_LOC),
    name="campaign_web_searcher",
    include_contents="none",
    description="Performs the crucial first pass of web research about the campaign guide.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""Role: You are a web research operator gathering raw market-research material.

    <INSTRUCTIONS>
    1.  **Access Queries:** Retrieve the list of web search queries from the `initial_campaign_queries` input in the <CONTEXT> block.
    2.  **Execute Research:** Use the `google_search` tool to exhaustively execute **all** retrieved queries.
    3.  **Report RAW Findings:** For each query, list the concrete facts, quotes, entities, competitors, data points, and sentiment you found, grouped by query. Do NOT write a polished report and do NOT omit specifics — the next agent needs the raw material to synthesize from. Plain text with light markdown is fine.
    </INSTRUCTIONS>

    <CONTEXT>
        <initial_campaign_queries>
        {initial_campaign_queries}
        </initial_campaign_queries>
    </CONTEXT>

    <CONTEXT_GUIDANCE>
    Capture material relevant to:
    -   **Target Audience Insights:** Pain points, current conversations, unmet needs, or aspirations relevant to the product. Assume the given `target_audience` description is correct.
    -   **Product/Market Landscape:** Competitive alternatives, common use cases, and general market sentiment around the product category.
    -   **Key Selling Point Validation:** Evidence, data, or public opinion that supports or contradicts the effectiveness of the intended key selling points.
    -   **Cultural Relevance:** Current trends or cultural shifts that could impact campaign messaging.
    </CONTEXT_GUIDANCE>

    ---
    ### Final Instruction
    **Output the raw findings grouped by query. Preserve specifics (names, competitors, numbers, direct quotes). Do not editorialize into a final report — that is the next agent's job.**
    """,
    tools=[google_search],
    output_key="campaign_web_search_raw",
    after_agent_callback=callbacks.collect_research_sources_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# WS2 split — synthesizer half: tool-free / planner-free. Reads the raw findings
# (optional `{...?}` so an empty searcher turn degrades to empty synthesis and the
# wrapper retries the whole pair rather than raising KeyError inside it) and shapes
# them into the existing consumer-facing report.
campaign_web_synthesizer = Agent(
    # Quota spread (#94/#101): campaign worker bucket from the DoE arm (no grounding here).
    model=build_gemini(_CA_WORKER, location=_CA_LOC),
    name="campaign_web_synthesizer",
    include_contents="none",
    description="Synthesizes the raw campaign findings into a structured strategic report.",
    instruction="""Role: You are a strategic market research analyst and synthesis expert. Your goal is to transform the raw web-research findings into an actionable, structured report for marketers.

    <INSTRUCTIONS>
    Synthesize **only** the data in <campaign_web_search_raw> and present it as a detailed, structured, and objective summary following the <REPORT_STRUCTURE> block.
    </INSTRUCTIONS>

    <CONTEXT>
        <campaign_web_search_raw>
        {campaign_web_search_raw?}
        </campaign_web_search_raw>
    </CONTEXT>

    <CONTEXT_GUIDANCE>
    The report should primarily focus on insights related to:
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
    output_key="campaign_web_search_insights",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)

campaign_search_and_synthesize = SequentialAgent(
    name="campaign_search_and_synthesize",
    description="Runs the raw web search then synthesizes the campaign report.",
    sub_agents=[campaign_web_searcher, campaign_web_synthesizer],
)

# Retry-on-empty: if the searcher OR synthesizer emits no final text (leaving
# `campaign_web_search_insights` unset), re-run the whole pair until the key is
# populated (bounded by max_attempts). The wrapper runs only sub_agents[0], so we
# wrap the SequentialAgent pair — the searcher re-runs too, which is the only way
# to recover a searcher that itself emptied.
campaign_web_searcher_resilient = RetryUntilKeyAgent(
    name="campaign_web_searcher_resilient",
    sub_agents=[campaign_search_and_synthesize],
    output_key="campaign_web_search_insights",
    max_attempts=3,
)

ca_sequential_planner = SequentialAgent(
    name="ca_sequential_planner",
    description="Executes sequential research tasks for concepts described in the campaign guide.",
    sub_agents=[campaign_web_planner, campaign_web_searcher_resilient],
)
