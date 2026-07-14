import logging
import warnings
from typing import Literal

from google.genai import types
from pydantic import BaseModel, Field
from google.adk.tools import google_search
from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.agents import Agent, SequentialAgent, ParallelAgent

from .sub_agents.campaign_researcher.agent import ca_sequential_planner
from .sub_agents.trend_researcher.agent import gs_sequential_planner
from agent_common import build_gemini, RetryUntilKeyAgent
from .config import config, INFRA_RETRY
from . import callbacks
from . import tools
from creative_eval.agent import creative_eval_agent


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


# --- PARALLEL RESEARCH SUBAGENTS --- #
parallel_planner_agent = ParallelAgent(
    name="parallel_planner_agent",
    sub_agents=[gs_sequential_planner, ca_sequential_planner],
    description="Runs multiple research planning agents in parallel.",
)

merge_planners = Agent(
    name="merge_planners",
    model=build_gemini(config.worker_model),
    include_contents="none",
    description="Combine results from state keys 'campaign_web_search_insights' and 'gs_web_search_insights'",
    instruction="""Role: You are an expert Strategic Synthesis Analyst. 
    Your core function is to critically analyze, cross-reference, and integrate two separate research reports (Campaign and Trend) into a single, cohesive, and actionable Strategic Brief for the creative team.

    <INSTRUCTIONS>
    1.  **Analyze and Integrate:** Carefully read the two provided research summaries (Campaign and Trend).
    2.  **Cross-Reference:** Identify areas of overlap or synergy between the campaign insights and the trend analysis (e.g., does the trend reinforce a key selling point?)
    3.  **Synthesize and Structure:** Generate a new, integrated Strategic Brief, following the structure and guidance in the <REPORT_STRUCTURE> block. **Do not simply paste the old reports.**
    4.  **Handle Missing Research:** If either the Campaign Insights or Trend Analysis section is empty, explicitly note the missing research in the brief (a short "Research Gaps" line) and synthesize from whatever is present — do not fabricate the missing report.
    </INSTRUCTIONS>

    <CONTEXT>
        The following research reports have been completed:
        - **Campaign Insights:** {campaign_web_search_insights?}
        - **Trend Analysis:** {gs_web_search_insights?}
    </CONTEXT>

    <REPORT_STRUCTURE>
    Your output must be a single, detailed, easy-to-read Strategic Brief sectioned with bold headings. The brief must synthesize the information to provide a clear path forward for creative development.

    1.  **Executive Summary (The Big Idea):** (A short, 2-3 sentence overview of the combined research. What is the single most important takeaway for the creative team?)
    2.  **Core Campaign Fundamentals:** (A synthesized summary of the Target Audience, Product Landscape, and Key Selling Points, drawing primarily from the Campaign Insights.)
    3.  **Cultural Opportunity & Relevance:** (An integrated analysis that connects the trending topic to the core campaign. How can the trend be used to make the campaign relevant? What specific tone, language, or narrative from the trend should be adopted?)
    4.  **Strategic Recommendations for Creative:** (Provide 3 specific, actionable directives for the ad copy and visual generation agents, based on the integrated findings. *Example: "Use 'X' phrase from the trend to frame 'Y' selling point."*

    ---
    ### Final Instruction
    **CRITICAL RULE: Output *only* the fully synthesized Strategic Brief in the format described in the <REPORT_STRUCTURE> block. Do not include the original content of the two input reports, and do not use introductory/concluding remarks outside of the suggested sections.**
    """,
    output_key="combined_web_search_insights",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)

# 5.  **Risk & Constraint:** (A final, integrated summary of any cultural risks (from the trend) or market constraints (from the campaign) the creative team must avoid.)
# </REPORT_STRUCTURE>

merge_parallel_insights = SequentialAgent(
    name="merge_parallel_insights",
    sub_agents=[parallel_planner_agent, merge_planners],
    description="Coordinates parallel research and synthesizes the results.",
)


# --- RESEARCH FEEDBACK SCHEMA --- #
class SearchQuery(BaseModel):
    """Model representing a specific search query for web search."""

    search_query: str = Field(
        description="A highly specific and targeted query for web search."
    )


class ResearchFeedback(BaseModel):
    """Model for providing evaluation feedback on research quality."""

    finding_type: Literal["Gap", "Opportunity"] = Field(
        description="Evaluation result. 'Gap' if gathering missing data is most critical, 'Opportunity' if the remaining research should focus on exploring the nuances of the overlap/sentiment."
    )

    analysis_comment: str = Field(
        description="Detailed explanation of the gap found OR the opportunity identified (max 3 sentences)."
    )

    follow_up_queries: list[SearchQuery] | None = Field(
        default=None,
        description="A list of specific, targeted follow-up search queries to either fill the identified gap or explore the highest-potential opportunity",
    )


combined_web_evaluator = Agent(
    model=build_gemini(config.critic_model),
    name="combined_web_evaluator",
    include_contents="none",
    description="Critically evaluates research about the campaign guide and generates follow-up queries.",
    instruction="""Role: You are a Lead Strategic Research Quality Assurance Analyst. 
    Your task is to critically review the combined research brief, identify any gaps or high-potential connections, and generate a final set of precise, high-signal follow-up queries.

    <INSTRUCTIONS>
    1.  **Critically Evaluate:** Analyze the Strategic Brief provided in the `<CONTEXT>` block. Assume the given `target_audience` description is exactly who we want to target. Do not question or try to verify the description itself.
    2.  **Gap Identification:** Determine if there is any missing information required to confidently connect the `<target_product>` and `<target_search_trends>` to the `<target_audience>`.
    3.  **Opportunity Assessment:** Identify the most promising *unexplored* connection or sentiment between the three core elements (Product, Trend, Audience).
    4.  **Query Generation:** Generate a final set of 5-7 high-signal web queries to either fill the identified gap or explore the highest-potential opportunity.
    5.  **Strict Output:** Produce a single, valid JSON object following the required schema, which includes both the analytical finding and the final queries.
    </INSTRUCTIONS>

    <CONTEXT>
        <combined_web_search_insights>
        {combined_web_search_insights}
        </combined_web_search_insights>

        <target_audience>
        {target_audience}
        </target_audience>

        <target_product>
        {target_product}
        </target_product>

        <target_search_trends>
        {target_search_trends}
        </target_search_trends>
    </CONTEXT>

    <GUIDANCE>
    1. Your analysis must yield a single, clear recommendation (Gap OR Opportunity).
       - **If a Gap is most critical:** Focus the follow-up queries on gathering the missing foundational data.
       - **If an Opportunity is most critical:** Focus the follow-up queries on exploring the nuances of the overlap/sentiment.
    2. All queries must be optimized for immediate web execution (i.e., short, specific, high-signal).
    </GUIDANCE>

    ---
    ### Output Format
    **STRICT RULE: Your entire output MUST be a single, raw JSON object validating against the 'ResearchFeedback' schema. Do not include any introductory text, analysis, or markdown outside the JSON block.**

    """,
    output_schema=ResearchFeedback,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    output_key="combined_research_evaluation",
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# WS2 split — searcher half: runs the follow-up google_search and emits RAW
# findings only. Separating tool-use from synthesis is the durable fix for the
# empty-turn flake. Grounding metadata lives on this turn, so
# `collect_research_sources_callback` stays here.
enhanced_combined_searcher = Agent(
    model=build_gemini(config.worker_model),
    name="enhanced_combined_searcher",
    include_contents="none",
    description="Executes follow-up searches and returns raw new findings.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""Role: You are a web research operator executing a final set of follow-up queries.

    <INSTRUCTIONS>
    1.  **Access Queries:** The follow-up queries are contained within the `combined_research_evaluation` JSON object in the `follow_up_queries` key.
    2.  **Execute Search:** Use the `google_search` tool to execute **all** queries from the `follow_up_queries` list.
    3.  **Report RAW Findings:** For each query, list the concrete new facts, quotes, entities, dates, and numbers you found, grouped by query. Do NOT write a polished summary and do NOT omit specifics — the next agent needs the raw material. Plain text with light markdown is fine.
    </INSTRUCTIONS>

    <CONTEXT>
        <combined_research_evaluation>
        {combined_research_evaluation}
        </combined_research_evaluation>
    </CONTEXT>

    ---
    ### Final Instruction
    **Output the raw new findings grouped by query. Preserve specifics. Do not editorialize into a final summary — that is the next agent's job.**
    """,
    tools=[google_search],
    output_key="refined_web_search_raw",
    after_agent_callback=callbacks.collect_research_sources_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# WS2 split — synthesizer half: tool-free / planner-free. Reads the raw follow-up
# findings (optional `{...?}` so an empty searcher turn degrades to empty synthesis
# and the wrapper retries the whole pair rather than raising KeyError inside it) and
# shapes them into the existing "New Research Findings" summary.
refined_web_synthesizer = Agent(
    model=build_gemini(config.worker_model),
    name="refined_web_synthesizer",
    include_contents="none",
    description="Synthesizes the raw follow-up findings into a concise new-insights summary.",
    instruction="""Role: You are a focused Research Refinement Specialist. Your sole task is to turn the raw follow-up findings into a concise summary of only the *new* insights discovered.

    <INSTRUCTIONS>
    Synthesize **only** the data in <refined_web_search_raw> into a **brief, structured summary** focusing *only* on the information that addresses the identified research gap or opportunity.
    </INSTRUCTIONS>

    <CONTEXT>
        <refined_web_search_raw>
        {refined_web_search_raw?}
        </refined_web_search_raw>
    </CONTEXT>

    <OUTPUT_FORMAT>
    **CRITICAL RULE: Your entire output MUST be a brief, new summary section using clear, bold headings. Do not include any introductory or concluding text.**

    # New Research Findings and Connections
    ## Key Insights Addressing Research Gap/Opportunity:
    (Present 3-5 concise bullet points summarizing the new data gathered.)
    </OUTPUT_FORMAT>

    """,
    output_key="refined_web_search_insights",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)

refined_search_and_synthesize = SequentialAgent(
    name="refined_search_and_synthesize",
    description="Runs the follow-up web search then synthesizes the new findings.",
    sub_agents=[enhanced_combined_searcher, refined_web_synthesizer],
)


# Retry-on-empty: if the searcher OR synthesizer emits no final text (leaving
# `refined_web_search_insights` unset), re-run the whole pair until populated
# (bounded). combined_report_composer already guards with
# `{refined_web_search_insights?}`, but retrying recovers the refinement (a
# quality gain) instead of silently dropping it. The wrapper runs only
# sub_agents[0], so we wrap the SequentialAgent pair.
enhanced_combined_searcher_resilient = RetryUntilKeyAgent(
    name="enhanced_combined_searcher_resilient",
    sub_agents=[refined_search_and_synthesize],
    output_key="refined_web_search_insights",
    max_attempts=3,
)


# `{refined_web_search_insights?}` is intentionally OPTIONAL (trailing `?`): the upstream
# enhanced_combined_searcher occasionally emits no final text (google_search + thinking
# returning only tool calls), leaving its output_key unset. Without the `?`, ADK raises
# `KeyError: Context variable not found` here and aborts the whole run after the expensive
# research. The refinement is additive — the full base research is in
# `{combined_web_search_insights}` — so degrading to an empty section is the right fallback.
combined_report_composer = Agent(
    model=build_gemini(config.critic_model),
    name="combined_report_composer",
    include_contents="none",
    description="Transforms research data and a markdown outline into a final, cited report.",
    instruction="""Role: You are the Lead Campaign Strategist. 
    Your final task is to generate the definitive and comprehensive research report by merging the initial Strategic Brief with the latest Refinement Findings. This report will directly inform the Ad Copy and Visual Generation teams.

    <INSTRUCTIONS>
    1.  **Review All Data:** Carefully review the initial Strategic Brief and the newly gathered Refinement Findings.
    2.  **Comprehensive Synthesis:** Integrate the new findings seamlessly into the original brief, paying close attention to addressing the initially identified research gap or exploring the opportunity.
    3.  **Final Report Structure:** Generate a final, polished Strategic Report following the structure outlined in the <FINAL_REPORT_STRUCTURE> block. Ensure the report fully addresses all core topics: Product, Trend, Audience, and their intersection.
    </INSTRUCTIONS>

    
    <CONTEXT>
        <combined_web_search_insights>
        {combined_web_search_insights}
        </combined_web_search_insights>

        <refined_web_search_insights>
        {refined_web_search_insights?}
        </refined_web_search_insights>

        <key_selling_points>
        {key_selling_points}
        </key_selling_points>

        <target_search_trends>
        {target_search_trends}
        </target_search_trends>

        <sources>
        {sources}
        </sources>
    </CONTEXT>


    <FINAL_REPORT_STRUCTURE>
    Your output **MUST** be a single, cohesive, comprehensive report delivered entirely in **Markdown format**.

    **Structure Mandate:**
    1.  The report must start with a single Level 1 Heading (`#`) for the Campaign Title.
    2.  Immediately following the title, you must include the Search Trend in bold: **Search Trend: {target_search_trends}**.
    3.  Each subsequent section must begin with a **Level 2 Markdown Heading (`##`)**, followed by an **introductory paragraph** (2-3 sentences) summarizing the content of the section, and then supported by **sub-headings (Level 3 or 4) or bullet points** to detail the key insights.

    **Mandatory Sections (following the Title and Trend Line):**

    1.  **## Executive Summary**
        *   (Introductory Paragraph: The single most critical creative takeaway/finding from all the research.)
        *   (Supporting bullets for the main points.)
    2.  **## Core Campaign Fundamentals**
        *   (Introductory Paragraph: Overview of the validated audience, product context, and primary selling points.)
        *   (Supporting bullets/sub-sections for Target Audience Profile, Product Landscape, and Confirmed Selling Points.)
    3.  **## Integrated Trend and Cultural Analysis**
        *   (Introductory Paragraph: The final analysis of the trend, its trajectory, and its validated connection to the campaign.)
        *   (Supporting bullets/sub-sections detailing the cultural narrative, relevance, and connection points.)
    4.  **## Actionable Creative Briefing Points**
        *   (Introductory Paragraph: Summary of the specific, high-priority creative directives.)
        *   (5 highly specific, validated recommendations for the Ad Copy and Visual teams, covering messaging, tone, and visual direction, presented as a numbered list or bullet points.)
        </FINAL_REPORT_STRUCTURE>

    ---
    **CRITICAL: Citation System**
    To cite a source, you MUST insert a special citation tag directly after the claim it supports.

    **The only correct format is:** `<cite source="src-ID_NUMBER" />`

    ---
    ### Final Instruction
    **CRITICAL RULE: Output *only* the fully synthesized Strategic Report in the requested Markdown format and using ONLY the `<cite source="src-ID_NUMBER" />` tag system for all citations. Ensure the structure strictly follows: Level 1 Title, Bold Search Trend Line, then the Level 2 Sections. Do not include any introductory or concluding remarks.**
    """,
    output_key="combined_final_cited_report",
    after_agent_callback=callbacks.citation_replacement_callback,
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)

# 5.  **## Final Risk Assessment & Constraints**
#     *   (Introductory Paragraph: Summary of any critical constraints or risks the creative team must avoid.)
#     *   (No more than 3 supporting bullets detailing the specific risks/constraints.)


# --- COMPLETE RESEARCH PIPELINE SUBAGENT --- #
combined_research_pipeline = SequentialAgent(
    name="combined_research_pipeline",
    description="Executes a pipeline of web research. It performs iterative research, evaluation, and insight generation.",
    sub_agents=[
        merge_parallel_insights,
        combined_web_evaluator,
        enhanced_combined_searcher_resilient,
        combined_report_composer,
    ],
)


# --- AD COPY SCHEMA ---
class AdCopy(BaseModel):
    """Model representing a single Ad Copy idea"""

    id: int = Field(description="Numerical identifier; use values 1-10.")
    tone_style: Literal[
        "Humorous",
        "Aspirational",
        "Problem/Solution",
        "Emotional/Authentic",
        "Educational/Informative",
        "Relatable/Meme-based",
    ] = Field(description="Specify one of the required tones.")
    headline: str = Field(description="A short, attention-grabbing Headline.")
    body_text: str = Field(
        description="2-3 sentences of concise and compelling ad copy."
    )
    trend_connection: str = Field(
        description="A sentence explaining how this copy leverages or references the trend: {target_search_trends}."
    )
    audience_appeal_rationale: str = Field(
        description="A brief, 1-sentence rationale for why this idea will appeal to the target audience, based on the research report."
    )
    social_caption: str = Field(
        description="A candidate, short social media caption (e.g., for Instagram or TikTok video description)."
    )


class AdCopyList(BaseModel):
    """Model for efficiently providing ad copy ideas for the critic agent to consume."""

    ad_copies: list[AdCopy] | None = Field(
        default=None,
        description="A list of 10 initial ad copy ideas.",
    )


# --- AD COPY AGENT (DRAFT) ---
ad_copy_drafter = Agent(
    model=build_gemini(config.worker_model),
    name="ad_copy_drafter",
    include_contents="none",
    description="Generate 10 initial ad copy ideas based on campaign guidelines and trends",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""Role: You are an innovative, fast-paced ad copy generator specializing in high-velocity social media content (Instagram/TikTok).

    Your task is to review the comprehensive research provided in the <CONTEXT> block and generate **10 distinct, culturally relevant ad copy ideas**.

    <INSTRUCTIONS>
    1.  **Analyze and Apply:** Analyze the research report to understand the audience, product, and trend intersection.
    2.  **Generate 10 Diverse Ideas:** Generate exactly 10 ad copy ideas. Each idea must:
        *   Creatively market the target product: {target_product}
        *   Incorporate the key selling point(s): {key_selling_points}
        *   Be suitable for Instagram/TikTok platforms (short, punchy, visual-friendly).
        *   Directly reference or subtly leverage the trending topic: {target_search_trends}.
    3.  **Enforce Creative Diversity:** To ensure variety, the 10 ideas must collectively cover at least 4 of the following creative tones/styles: **Humorous, Aspirational, Problem/Solution, Emotional/Authentic, Educational/Informative, Relatable/Meme-based.**
    4.  **Strict Output Format:** Ensure the entire output is a single JSON object containing all 10 ideas, formatted exactly as specified in the <OUTPUT_FORMAT> block.
    </INSTRUCTIONS>

    <CONTEXT>
        <combined_final_cited_report>
        {combined_final_cited_report}
        </combined_final_cited_report>
    </CONTEXT>

    <OUTPUT_FORMAT>
    **CRITICAL RULE: Your entire output MUST be a single, raw JSON object validating against the 'AdCopyList' schema**
    </OUTPUT_FORMAT>
    """,
    generate_content_config=types.GenerateContentConfig(
        temperature=1.5,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "ad_copy_drafter",
        },
    ),
    output_schema=AdCopyList,
    output_key="ad_copy_draft",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# --- AD COPY SCHEMA FINAL ---
class FinalAdCopy(BaseModel):
    """Model representing a single Ad Copy idea"""

    original_id: int = Field(description="Retain the original ID for traceability.")
    tone_style: Literal[
        "Humorous",
        "Aspirational",
        "Problem/Solution",
        "Emotional/Authentic",
        "Educational/Informative",
        "Relatable/Meme-based",
    ] = Field(description="The tone/style from the original idea (e.g., Humorous).")
    headline: str = Field(description="The finalized, attention-grabbing Headline.")
    body_text: str = Field(description="The finalized, concise and compelling ad copy.")
    trend_connection: str = Field(
        description="A sentence explaining how this copy leverages or references the trend: {target_search_trends}."
    )
    audience_appeal_rationale: str = Field(
        description="A brief, 1-sentence rationale for target audience appeal."
    )
    social_caption: str = Field(
        description="The finalized candidate social media caption."
    )
    call_to_action: str = Field(
        description="A NEW, catchy, action-oriented phrase (e.g., 'Shop the drop now!')."
    )
    detailed_performance_rationale: str = Field(
        description="A 2-3 sentence strategic critique explaining *why* this ad copy will perform well against the selection criteria."
    )


class FinalAdCopyList(BaseModel):
    """Model for efficiently providing ad copy ideas for the critic agent to consume."""

    ad_copies: list[FinalAdCopy] | None = Field(
        default=None,
        description="A list of the finalized ad copy ideas.",
    )


# --- AD COPY CRITIC AGENT ---
ad_copy_critic = Agent(
    model=build_gemini(config.critic_model),
    name="ad_copy_critic",
    include_contents="none",
    description="Critique and narrow down ad copies based on product, audience, and trends",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""Role: You are a strategic marketing critic and conversion optimization expert. 
    Your task is to apply rigorous analysis to candidate ad copy ideas and select a final, high-potential subset for creative development.

    <INSTRUCTIONS>
    1.  **Parse Input:** Retrieve and parse the JSON list of 10 ad copies from the `ad_copy_draft` input in the <CONTEXT> block.
    2.  **Critical Evaluation:** Evaluate the 10 ideas based on the following criteria:
        *   **Strategic Alignment:** How well does the idea synthesize the product, key selling points, and target audience insights from the research report?
        *   **Trend Authenticity:** Does the use of the trending topic feel natural, relevant, and not forced?
        *   **Platform Viability:** Is the tone and length highly suitable for Instagram/TikTok?
        *   **Creative Excellence:** Is the idea compelling, clear, and likely to drive a high click-through rate?
    3.  **Final Selection:** Select a subset of **exactly 4** ad copy ideas that demonstrate the highest potential.
    4.  **Enrich and Critique:** For each selected idea, you must add a high-converting **Call-to-Action (CTA)** and a **Detailed Rationale** explaining the strategic choice.
    5.  **Strict Output:** Output the final selection as a single JSON object, strictly following the schema in the `<OUTPUT_FORMAT>` block.
    </INSTRUCTIONS>

    <CONTEXT>
        <target_search_trends>
        {target_search_trends}
        </target_search_trends>

        <ad_copy_draft>
        {ad_copy_draft}
        </ad_copy_draft>
    </CONTEXT>

    <OUTPUT_FORMAT>
    **CRITICAL RULE: Your entire output MUST be a single, raw JSON object validating against the 'FinalAdCopyList' schema**
    <OUTPUT_FORMAT>
    """,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.7,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "ad_copy_critic",
        },
    ),
    output_schema=FinalAdCopyList,
    output_key="ad_copy_critique",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# Sequential agent for ad creative generation
ad_creative_pipeline = SequentialAgent(
    name="ad_creative_pipeline",
    description="Generates ad copy drafts with an actor-critic workflow.",
    sub_agents=[
        ad_copy_drafter,
        ad_copy_critic,
    ],
)


# --- VISUAL CONCEPT SCHEMA ---
class VisualConcept(BaseModel):
    """Model representing a single candidate visual concept."""

    ad_copy_id: int = Field(
        description="Retains the original ID for a direct link to the ad copy."
    )
    concept_name: str = Field(
        description="A short, intuitive name for the visual concept."
    )
    trend_visual_link: str = Field(
        description="A 1-sentence description of how the visual specifically incorporates the {target_search_trends}."
    )
    concept_summary: str = Field(
        description="A 2-3 sentence explanation of the creative concept and its link to the ad copy's message."
    )
    image_generation_prompt: str = Field(
        description="A draft prompt for image generation."
    )


class VisualConceptList(BaseModel):
    """Model listing all initial visual concepts."""

    visual_concepts: list[VisualConcept] | None = Field(
        default=None,
        description="A list of candidate visual concepts.",
    )


# --- VISUAL CONCEPT DRAFT AGENT ---
visual_concept_drafter = Agent(
    model=build_gemini(config.worker_model),
    name="visual_concept_drafter",
    include_contents="none",
    description="Generate initial visual concepts for selected ad copies",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""Role: You are a visionary visual creative director and prompt engineer specializing in high-impact social media advertising (Instagram/TikTok). 
    Your task is to translate approved ad copy into executable visual concepts.

    <INSTRUCTIONS>
    1.  **Parse and Map:** Parse the JSON list of final ad copies from the `ad_copy_critique` input in the <CONTEXT> block.
    2.  **Concept Generation:** For *each* ad copy, generate exactly one distinct visual concept. The concept must:
        *   Be a direct, visual representation of the core ad message (headline + body).
        *   Leverage or subtly reference the trending topic: {target_search_trends}.
        *   Be optimized for quick consumption on a social media feed (e.g., strong composition, clear focus).
        *   Cleverly market the target product: {target_product}.
    3.  **Prompt Engineering:** For each concept, generate a professional, high-fidelity text-to-image generation prompt adhering to the <PROMPT_ENGINEERING_GUIDANCE> block.
    4.  **Strict Output Format:** Ensure the entire output is a single JSON object containing all generated concepts, strictly following the schema in the <OUTPUT_FORMAT> block.
    </INSTRUCTIONS>

    <CONTEXT>
        <ad_copy_critique>
        {ad_copy_critique}
        </ad_copy_critique>
    </CONTEXT>

    <PROMPT_ENGINEERING_GUIDANCE>
    The final generated prompt for the image model must be:
    -   **Highly descriptive:** Include subject, setting, style, mood, and lighting.
    -   **Technical:** Specify aspect ratio (e.g., 9:16 for vertical), camera angle, and lens type (e.g., telephoto, wide-angle).
    -   **Optimized:** Use high-quality keywords (e.g., "photorealistic," "award-winning studio lighting," "8k resolution").
    </PROMPT_ENGINEERING_GUIDANCE>

    <OUTPUT_FORMAT>
    **CRITICAL RULE: Your entire output MUST be a single, raw JSON object validating against the 'VisualConceptList' schema**
    </OUTPUT_FORMAT>
    """,
    generate_content_config=types.GenerateContentConfig(
        temperature=1.5,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_concept_drafter",
        },
    ),
    output_schema=VisualConceptList,
    output_key="visual_draft",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# --- VISUAL CONCEPT CRITIQUE SCHEMA ---
class VisualConceptCritique(BaseModel):
    """Model representing the critique of a single candidate visual concept."""

    ad_copy_id: int = Field(
        description="Retains the original ID for a direct link to the ad copy."
    )
    concept_name: str = Field(description="The original visual concept name.")
    trend_visual_link: str = Field(description="The original trend link description.")
    concept_summary: str = Field(
        description="The original creative concept explanation."
    )
    image_generation_prompt: str = Field(
        description="The FINAL, technically perfected, 100+ word, high-fidelity prompt."
    )
    critique_summary: str = Field(
        description="A brief (1-2 sentence) summary of the key technical changes made to the prompt."
    )


class VisualConceptCritiqueList(BaseModel):
    """Model listing all initial visual concepts."""

    visual_concepts: list[VisualConceptCritique] | None = Field(
        default=None,
        description="A list of visual concept critiques.",
    )


# --- VISUAL CONCEPT CRITIQUE AGENT ---
visual_concept_critic = Agent(
    model=build_gemini(config.critic_model),
    name="visual_concept_critic",
    include_contents="none",
    description="Critique and narrow down visual concepts",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""Role: You are an expert Visual Prompt Engineer and Creative Quality Assurance Specialist. 
    Your task is to apply rigorous technical and creative analysis to a set of draft image generation prompts, refining them for maximum visual impact and adherence to the core brief.

    <INSTRUCTIONS>
    1.  **Parse and Map:** Retrieve and parse the JSON list of visual concepts from the **`<CONTEXT>` block's `visual_draft`** input.
    2.  **Critical Review and Revision:** For each concept, critique and **REWRITE** the `image_generation_prompt` based on the following criteria:
        *   **Technical Compliance:** Ensure the prompt is over **100 words**, uses high-fidelity keywords, specifies aspect ratio, and clearly defines lighting, style, and composition elements (as per prompt best practices).
        *   **Creative Fidelity:** Ensure the revised prompt vividly describes the **{target_product}** and makes a clear visual link to the **{target_search_trends}** trend in a way that aligns with the intended tone.
        *   **Stopping Power:** The resulting image must have high visual appeal and "stopping power" for a social media feed.
    3.  **Strict Output Format:** The output must be a single, structured JSON object containing the **revised** concepts. Do not include any external commentary or separate critique text.
    </INSTRUCTIONS>

    <CONTEXT>
        <visual_draft>
        {visual_draft}
        </visual_draft>
    </CONTEXT>

    <OUTPUT_FORMAT>
    **CRITICAL RULE: Your entire output MUST be a single, raw JSON object validating against the 'VisualConceptCritiqueList' schema**
    </OUTPUT_FORMAT>
    """,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.7,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_concept_critic",
        },
    ),
    output_schema=VisualConceptCritiqueList,
    output_key="visual_concept_critique",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# --- VISUAL CONCEPT FINAL SCHEMA ---
class VisualConceptFinal(BaseModel):
    """Model representing a finalized visual concept."""

    ad_copy_id: int = Field(
        description="Retains the original ID for a direct link to the ad copy."
    )
    concept_name: str = Field(description="The finalized name of the visual concept.")
    trend: str = Field(description="The trend referenced by this visual concept.")
    trend_reference: str = Field(
        description="How the visual concept relates to the target search trend"
    )
    markets_product: str = Field(
        description="A brief explanation of how this markets the target product"
    )
    audience_appeal: str = Field(
        description="A brief explanation for the target audience appeal."
    )
    selection_rationale: str = Field(
        description="A brief rationale explaining why this visual concept was selected and why it will perform well"
    )
    headline: str = Field(
        description="The final Headline text from the original ad copy."
    )
    social_caption: str = Field(
        description="The final social media caption from the original ad copy."
    )
    call_to_action: str = Field(
        description="The final Call-to-Action from the original ad copy."
    )
    concept_summary: str = Field(
        description="A final, brief (2-3 sentence) summary of the combined ad copy and visual concept."
    )
    image_generation_prompt: str = Field(
        description="The technically perfected, revised_image_generation_prompt."
    )


class VisualConceptFinalList(BaseModel):
    """Model listing all finalized visual concepts."""

    visual_concepts: list[VisualConceptFinal] | None = Field(
        default=None,
        description="A list of finalized visual concept.",
    )


# --- VISUAL CONCEPT FINAL AGENT ---
visual_concept_finalizer = Agent(
    model=build_gemini(config.worker_model),
    name="visual_concept_finalizer",
    include_contents="none",
    description="Finalize visual concepts to proceed with.",
    instruction="""Role: You are the Lead Creative Director and Final Gatekeeper. 
    Your task is to apply ultimate strategic judgment to the final set of visual concepts, selecting the absolute best for production (image generation).

    <INSTRUCTIONS>
    1.  **Parse and Map:** Retrieve and parse the JSON list of revised visual concepts from the **`<CONTEXT>` block's `visual_concept_critique` input.
    2.  **Final Selection Criteria:** Select a subset of **exactly 4** concepts that offer the best balance of:
        *   **Creative Diversity:** Ensure the final 4 represent a good mix of styles/tones from the original ad copy set.
        *   **Commercial Viability:** Highest potential to drive engagement and sales, based on the `critique_summary`.
        *   **Technical Excellence:** Possesses the most compelling and robust `revised_image_generation_prompt`.
    3.  **Finalize and Enrich:** For the 4 selected concepts, you must combine the original ad copy details with the revised visual details to create a final, unified creative brief.
    4.  **Strict Output Format:** Output the final selection as a single JSON object, strictly following the schema in the `<OUTPUT_FORMAT>` block.
    </INSTRUCTIONS>

    <CONTEXT>
        <visual_concept_critique>
        {visual_concept_critique}
        </visual_concept_critique>

        <ad_copy_critique>
        {ad_copy_critique}
        </ad_copy_critique>
    </CONTEXT>

    <GUIDANCE>
    Each visual concept has an `ad_copy_id` that maps to an entry in `ad_copy_critique`.
    You MUST look up the matching ad copy by `original_id` and use its exact `headline`, `social_caption`, and `call_to_action` values — do NOT generate new ones.
    </GUIDANCE>

    <OUTPUT_FORMAT>
    **CRITICAL RULE: Your entire output MUST be a single, raw JSON object validating against the 'VisualConceptFinalList' schema**
    </OUTPUT_FORMAT>
    """,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.8,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_concept_finalizer",
        },
    ),
    output_schema=VisualConceptFinalList,
    output_key="final_visual_concepts",
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# --- VISUAL GENERATOR AGENT ---
# Runs generate_image over the finalized visual concepts. In creative_agent it is
# chained into visual_production_pipeline (below) so image rendering is deterministic
# and the orchestrator cannot skip it. interactive_creative deliberately invokes it as
# a separate step AFTER a human review checkpoint (review concepts before spending on
# image generation), so it must also remain usable as a standalone agent.
visual_generator = Agent(
    model=build_gemini(config.critic_model),
    name="visual_generator",
    retry_config=INFRA_RETRY,
    include_contents="none",  # new
    description="Generate final visuals using image generation tools",
    # thinking_budget=0: this is a mechanical single-tool step, not a reasoning task.
    # Without it, gemini-3 would emit MULTIPLE parallel `generate_image` calls in one
    # turn — and parallel calls all read state before any commits, so the tool's
    # idempotency guard (_images_generated) can't dedupe them, causing every image to
    # be rendered 2x (double the image-gen cost). One call is all that's needed:
    # generate_image itself loops over every concept in final_visual_concepts.
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(thinking_budget=0, include_thoughts=False)
    ),
    instruction="""You are a visual content producer generating image creatives.
    Call the `generate_image` tool EXACTLY ONCE — a single function call, never in
    parallel and never more than once. It renders images for all concepts on its own.
    After it returns, reply with a one-line confirmation. Do not call it again.
    """,
    tools=[tools.generate_image],
    generate_content_config=types.GenerateContentConfig(
        temperature=1.2,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "visual_generator",
        },
    ),
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
)


# Sequential agent for visual concepts (draft -> critique -> finalize). Shared with
# interactive_creative, which pauses for human review after this stage before rendering.
visual_generation_pipeline = SequentialAgent(
    name="visual_generation_pipeline",
    description="Generates visual concepts with an actor-critic workflow.",
    sub_agents=[
        visual_concept_drafter,
        visual_concept_critic,
        visual_concept_finalizer,
    ],
)


# creative_agent (non-interactive) renders images immediately after finalizing
# concepts, as one deterministic unit. This removes the orchestrator's opportunity to
# skip image generation — which it did when creative_eval_agent looked like the next
# step, jumping straight from visual concepts to evaluation. interactive_creative does
# NOT use this: it keeps concepts and images split around a review checkpoint.
visual_production_pipeline = SequentialAgent(
    name="visual_production_pipeline",
    description="Generate visual concepts, then render their image creatives.",
    sub_agents=[
        visual_generation_pipeline,
        visual_generator,
    ],
)


# --- MAIN ORCHESTRATOR AGENT ---
root_agent = Agent(
    model=build_gemini(config.critic_model),
    name="root_agent",
    retry_config=INFRA_RETRY,
    description="Help with ad generation; brainstorm and refine ad copy and visual concept ideas with actor-critic workflows; generate final ad creatives.",
    instruction="""**Role:** You are the orchestrator for a comprehensive ad content generation workflow.

    **Objective:** Your goal is to generate a complete set of ad creatives including ad copy and images, using the **provided campaign metadata inputs**. To achieve this, strictly use the <AVAILABLE_TOOLS/> available to complete the <INSTRUCTIONS/> below.


    <AVAILABLE_TOOLS>
    1. Use the `memorize` tool to store trends and campaign metadata in the session state.
    2. Use the `combined_research_pipeline` tool to conduct web research on the campaign metadata and selected trends.
    3. Use the `save_draft_report_artifact` tool to save a research PDf report to Cloud Storage.
    4. Use the `ad_creative_pipeline` tool to generate ad copies.
    5. Use the `visual_production_pipeline` tool to generate visual concepts and render their image creatives.
    6. Use the `creative_eval_agent` tool to evaluate all generated ad copies and visual concepts for quality.
    7. Use the `save_eval_report_to_gcs` tool to save the creative evaluation report JSON to Cloud Storage.
    8. Use the `save_creative_gallery_html` tool to build an HTML file for displaying a portfolio of the generated creatives generated during the session.
    9. Use the `write_trends_to_bq` tool to insert rows to BigQuery.
    10. Use the `write_eval_report_to_bq` tool to log the evaluation summary (pass rate, average scores, weakest dimensions) to BigQuery.
    </AVAILABLE_TOOLS>


    <INPUT_PARAMETERS>
    The following campaign metadata will be provided as input to this agent. You must receive and store these values before proceeding to the <WORKFLOW/>.
    - brand: [string] The client's brand name.
    - target_audience: [string] The specific demographic or group the ad is targeting.
    - target_product: [string] The name of the product or service being advertised.
    - key_selling_points: [string] The main benefits or features to highlight.
    - target_search_trends: [string] Trending topics or keywords relevant to the campaign.
    </INPUT_PARAMETERS>

    <INSTRUCTIONS>
    1. First, **receive and validate** the inputs defined in the <INPUT_PARAMETERS> block. If any critical input is missing (brand, target_audience, target_product, key_selling_points), respond with an error and halt execution.
    2. Use the `memorize` tool to store **all** the validated input campaign metadata into the corresponding session state variables: `brand`, `target_audience`, `target_product`, `key_selling_points`, and `target_search_trends`. Call the `memorize` tool for ALL of them in a single turn (or as parallel calls).
    3. Once all metadata is successfully stored in the session state, strictly follow all steps in the <WORKFLOW/> block one-by-one.
    </INSTRUCTIONS>


    <WORKFLOW>
    1. First, use the `combined_research_pipeline` tool to conduct web research, leveraging the stored campaign metadata and trends.
    2. Once all research tasks are complete, use the `save_draft_report_artifact` tool to save the research as a markdown file in Cloud Storage.
    3. Invoke the `ad_creative_pipeline` tool to generate a set of candidate ad copies.
    4. Then, call the `visual_production_pipeline` tool to generate visual concepts for the finalized ad copies and render high-fidelity image creatives for each concept.
    5. Call the `creative_eval_agent` tool to evaluate the quality of all generated ad copies and visual concepts. This will score each creative on dimensions like trend authenticity, copy quality, audience fit, and stopping power, and store a detailed evaluation report in the session state.
    6. Call the `save_eval_report_to_gcs` tool to save the creative evaluation report JSON to Cloud Storage.
    7. Then, call the `save_creative_gallery_html` tool to create an HTML portfolio and save it to Cloud Storage.
    8. Call the `write_trends_to_bq` tool to save trend information to BigQuery for logging and analytics.
    9. Finally as the last persistence step, call the `write_eval_report_to_bq` tool to log the evaluation summary (pass rate, average scores, weakest dimensions) to BigQuery for analytics.
    10. Once the previous steps are complete, perform the following action:

    Action 1: Display Cloud Storage location to the user
    Display the Cloud Storage URI to the user by combining the 'gcs_bucket', 'gcs_folder', and 'agent_output_dir' state keys like this: {gcs_bucket}/{gcs_folder}/{agent_output_dir}
    </WORKFLOW>

    Your job is complete when all tasks in the <WORKFLOW> block are complete and the final Cloud Storage URI has been displayed.
    """,
    tools=[
        AgentTool(agent=combined_research_pipeline),
        AgentTool(agent=ad_creative_pipeline),
        AgentTool(agent=visual_production_pipeline),
        AgentTool(agent=creative_eval_agent),
        tools.save_eval_report_to_gcs,
        tools.save_draft_report_artifact,
        tools.save_creative_gallery_html,
        tools.write_trends_to_bq,
        tools.write_eval_report_to_bq,
        tools.memorize,
    ],
    generate_content_config=types.GenerateContentConfig(
        temperature=1.0,
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "root_agent",
        },
    ),
    before_agent_callback=callbacks.load_session_state,
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=callbacks.log_empty_turn_finish_reason,
    after_agent_callback=callbacks.log_final_state_summary,
)

# To ensure correct state management, **chain the calls** such that you only call the next `memorize` after the previous call has successfully responded.
