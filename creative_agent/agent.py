import datetime, logging

logging.basicConfig(level=logging.INFO)

from google.genai import types
from pydantic import BaseModel, Field
from google.adk.tools import google_search
from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.agents import Agent, SequentialAgent, ParallelAgent

from .sub_agents.campaign_researcher.agent import ca_sequential_planner
from .sub_agents.trend_researcher.agent import gs_sequential_planner
from .config import config
from . import callbacks
from .tools import (
    memorize,
    generate_image,
    save_select_ad_copy,
    save_select_visual_concept,
    save_draft_report_artifact,
    save_creatives_html_report,
    save_creative_gallery_html,
    save_session_state_to_gcs,
)


# --- PARALLEL RESEARCH SUBAGENTS --- #
parallel_planner_agent = ParallelAgent(
    name="parallel_planner_agent",
    sub_agents=[gs_sequential_planner, ca_sequential_planner],
    description="Runs multiple research planning agents in parallel.",
)

merge_planners = Agent(
    name="merge_planners",
    model=config.worker_model,
    # include_contents="none",
    description="Combine results from state keys 'campaign_web_search_insights' and 'gs_web_search_insights'",
    instruction="""You are an AI Assistant responsible for combining initial research findings into a comprehensive summary.
    Your primary task is to organize the following research summaries, clearly attributing findings to their source areas. 

    <INSTRUCTIONS>
    To complete the task, you need to follow these steps:
    1. Structure your response using headings for each topic.
    2. Ensure the report is coherent and integrates the key points smoothly.
    3. The output format should include section headers like those described in the <OUTPUT_FORMAT> section.
    </INSTRUCTIONS>

    <CONSTRAINTS>
    Dos and don'ts for the following aspects
    1. Do not include introductory or concluding phrases outside the suggested structure
    2. Strictly adhere to using only the provided input summary content for each section.
    </CONSTRAINTS>

    <OUTPUT_FORMAT>
    # Summary of Campaign and Trend Research

    ## Campaign Guide
    {campaign_web_search_insights}

    ## Search Trend
    {gs_web_search_insights}
    </OUTPUT_FORMAT>

    <RECAP>
    Output *only* the structured report following the described format.
    </RECAP>
    """,
    output_key="combined_web_search_insights",
)

merge_parallel_insights = SequentialAgent(
    name="merge_parallel_insights",
    sub_agents=[parallel_planner_agent, merge_planners],
    description="Coordinates parallel research and synthesizes the results.",
)


# --- COMBINED RESEARCH SUBAGENTS --- #
# =============================
# Research Structured Feedback
# =============================
class CampaignSearchQuery(BaseModel):
    """Model representing a specific search query for web search."""

    search_query: str = Field(
        description="A highly specific and targeted query for web search."
    )


class CampaignFeedback(BaseModel):
    """Model for providing evaluation feedback on research quality."""

    comment: str = Field(
        description="Detailed explanation of the evaluation, highlighting strengths and/or weaknesses of the research."
    )
    follow_up_queries: list[CampaignSearchQuery] | None = Field(
        default=None,
        description="A list of specific, targeted follow-up search queries needed to fix research gaps. This should be null or empty if no follow-up questions needed.",
    )


combined_web_evaluator = Agent(
    model=config.critic_model,
    name="combined_web_evaluator",
    description="Critically evaluates research about the campaign guide and generates follow-up queries.",
    instruction=f"""
    You are a meticulous quality assurance analyst evaluating the research findings in 'combined_web_search_insights'.
    
    Be critical of the completeness of the research.
    Consider the bigger picture and the intersection of the `target_product` and `target_audience`. 
    Consider the trend in the 'target_search_trends' state key.
    
    Look for any gaps in depth or coverage, as well as any areas that need more clarification. 
        - If you find significant gaps in depth or coverage, write a detailed comment about what's missing, and generate 5-7 specific follow-up queries to fill those gaps.
        - If you don't find any significant gaps, write a detailed comment about any aspect of the campaign guide or trends to research further. Provide 5-7 related queries.

    Current date: {datetime.datetime.now().strftime("%Y-%m-%d")}
    Your response must be a single, raw JSON object validating against the 'CampaignFeedback' schema.
    """,
    output_schema=CampaignFeedback,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    output_key="combined_research_evaluation",
    before_model_callback=callbacks.rate_limit_callback,
)


enhanced_combined_searcher = Agent(
    model=config.worker_model,
    name="enhanced_combined_searcher",
    description="Executes follow-up searches and integrates new findings.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""
    You are a specialist researcher executing a refinement pass.
    You are tasked to conduct a second round of web research and gather insights related to the trending Search terms, the target audience, and the target product.

    1.  Review the 'combined_research_evaluation' state key to understand the previous round of research.
    2.  Execute EVERY query listed in 'follow_up_queries' using the 'google_search' tool.
    3.  Synthesize the new findings and COMBINE them with the existing information in 'combined_web_search_insights'.
    4.  Your output MUST be the new, complete, and improved set of research insights for the trending Search terms and campaign guide.
    """,
    tools=[google_search],
    output_key="combined_web_search_insights",
    after_agent_callback=callbacks.collect_research_sources_callback,
)


combined_report_composer = Agent(
    model=config.critic_model,
    name="combined_report_composer",
    include_contents="none",
    description="Transforms research data and a markdown outline into a final, cited report.",
    instruction="""
    Transform the data provided in the <CONTEXT> section into a polished, professional, and meticulously cited research report.

    
    <CONTEXT>
    *   **Search Trends:**
        {target_search_trends}
    
    *   **Final Research:**
        {combined_web_search_insights}
    
    *   **Citation Sources:** 
        `{sources}`
    </CONTEXT>

    ---
    **CRITICAL: Citation System**
    To cite a source, you MUST insert a special citation tag directly after the claim it supports.

    **The only correct format is:** `<cite source="src-ID_NUMBER" />`

    ---
    <OUTPUT_FORMAT>
    Organize the output to include these sections:
    *   **Campaign Guide**
    *   **Search Trend**
    *   **Key Insights from Research**

    You can use any format you prefer, but here's a suggested structure:
    # Campaign Title
    ## Section Name
    An overview of what this section covers, including specific insights from web research.
    Feel free to add subsections or bullet points if needed to better organize the content.
    Make sure your outline is clear and easy to follow.
    </OUTPUT_FORMAT>

    ---
    **Final Instructions**
    Generate a comprehensive report using ONLY the `<cite source="src-ID_NUMBER" />` tag system for all citations.
    Ensure the final report follows a structure similar to the one proposed in the <OUTPUT_FORMAT> section.
    Do not include a "References" or "Sources" section; all citations must be in-line.
    """,
    output_key="combined_final_cited_report",
    after_agent_callback=callbacks.citation_replacement_callback,
    before_model_callback=callbacks.rate_limit_callback,
)


# --- COMPLETE RESEARCH PIPELINE SUBAGENT --- #
combined_research_pipeline = SequentialAgent(
    name="combined_research_pipeline",
    description="Executes a pipeline of web research. It performs iterative research, evaluation, and insight generation.",
    sub_agents=[
        merge_parallel_insights,
        combined_web_evaluator,
        enhanced_combined_searcher,
        combined_report_composer,
    ],
)


# --- AD CREATIVE SUBAGENTS ---
ad_copy_drafter = Agent(
    model=config.worker_model,
    name="ad_copy_drafter",
    description="Generate 10 initial ad copy ideas based on campaign guidelines and trends",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""You are a creative copywriter generating initial ad copy ideas.

    Your task is to review the research and trend provided in the <CONTEXT> block and generate 10 culturally relevant ad copy ideas.

    <INSTRUCTIONS>
    To complete the task, you need to follow these steps:
    1. Using insights related to the campaign and Search trend, generate 10 diverse ad copy ideas that:
        - Creatively market the target product: {target_product}
        - Incorporate the following key selling point(s): {key_selling_points}
        - Vary in tone, style, and approach
        - Are suitable for Instagram/TikTok platforms
        - Reference the trending Search term: {target_search_trends}.
    2. **Each ad copy should include:**
        - Headline (attention-grabbing)
        - Body text (concise and compelling)
        - How it relates to the trending topic: {target_search_trends}
        - Brief rationale for target audience appeal
        - A candidate social media caption
    </INSTRUCTIONS>

    <CONTEXT>
        <target_search_trends>
        {target_search_trends}
        </target_search_trends>
        
        <combined_final_cited_report>
        {combined_final_cited_report}
        </combined_final_cited_report>
    </CONTEXT>

    Use the `google_search` tool to support your decisions.
    """,
    generate_content_config=types.GenerateContentConfig(
        temperature=1.5,
    ),
    tools=[google_search],
    output_key="ad_copy_draft",
)
# - Call-to-action


ad_copy_critic = Agent(
    model=config.critic_model,
    name="ad_copy_critic",
    description="Critique and narrow down ad copies based on product, audience, and trends",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""You are a strategic marketing critic evaluating ad copy ideas.

    Your task is to review candidate ad copies and select the 6 BEST ideas

    <INSTRUCTIONS>
    To complete the task, you need to follow these steps:
    1. Review the proposed candidates in the 'ad_copy_draft' state key.
    2. Select the 6 best ad copy ideas based on the following criteria:
        - Alignment with target audience.
        - Effective use of trending topic that feels authentic.
        - Clear communication of key selling points.
        - Platform-appropriate tone and length.
    3. Provide detailed rationale for your selections, explaining why these specific copies will perform best.
    </INSTRUCTIONS>
    
    <OUTPUT_FORMAT>
    Each ad copy should include:
        - Headline (attention-grabbing)
        - Body text (concise and compelling)
        - A candidate social media caption
        - Call-to-action (catchy, action-oriented phrase intended for target audience.)
        - How it relates to the trending topic: {target_search_trends}
        - Brief rationale for target audience appeal
        - Detailed rationale explaining why this ad copy will perform well
    </OUTPUT_FORMAT>
    """,
    # tools=[google_search],
    generate_content_config=types.GenerateContentConfig(temperature=0.7),
    output_key="ad_copy_critique",
)
# - Name (intuitive name of the ad copy idea)


# Sequential agent for ad creative generation
ad_creative_pipeline = SequentialAgent(
    name="ad_creative_pipeline",
    description="Generates ad copy drafts with an actor-critic workflow.",
    sub_agents=[
        ad_copy_drafter,
        ad_copy_critic,
    ],
)


# --- PROMPT GENERATION SUBAGENTS ---
visual_concept_drafter = Agent(
    model=config.worker_model,
    name="visual_concept_drafter",
    description="Generate initial visual concepts for selected ad copies",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""You are a visual creative director generating initial visual concepts for given ad copy. 

    Your task is to review the ad copies in the <CONTEXT> block and generate a culturally relevant visual concept for each.

    <CONTEXT>
        <ad_copy_critique>
        {ad_copy_critique}
        </ad_copy_critique>
    </CONTEXT>

    <INSTRUCTIONS>
    To complete the task, you need to follow these steps:
    1. Based on the selected ad copies in the <CONTEXT> block, generate visual concepts that follow these criteria:
        - Incorporate trending visual styles and themes.
        - Consider platform-specific best practices.
        - Find a clever way to market the target product: {target_product}
        - References the Search trend: {target_search_trends}
    2. Generate one visual concept for each ad copy.
    </INSTRUCTIONS>

    <OUTPUT_FORMAT>
    For each visual concept, provide:
    -   Name (intuitive name of the concept)
    -   How it relates to the Search trend: {target_search_trends}
    -   Which ad copy it connects to
    -   Creative concept explanation
    -   A draft prompt for image generation
    </OUTPUT_FORMAT>

    Use the `google_search` tool to support your decisions.
    """,
    tools=[google_search],
    generate_content_config=types.GenerateContentConfig(temperature=1.5),
    output_key="visual_draft",
)


visual_concept_critic = Agent(
    model=config.critic_model,
    name="visual_concept_critic",
    description="Critique and narrow down visual concepts",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""You are a creative director evaluating image generation prompts for visual concepts.

    Your task is to critique the prompts in the proposed visual concept drafts. Your objective is to generate high quality prompts that result in high impact.

    <CONTEXT>
        <visual_draft>
        {visual_draft}
        </visual_draft>
    </CONTEXT>

    <INSTRUCTIONS>
    To complete the task, you need to follow these steps:
    1. Review the visual concept drafts in the <CONTEXT> block and critique the image generation prompt on the following criteria:
        - Target descriptive image prompts that visualize the ad copy concepts
        - Ensure each prompt includes the subject, context/background, and style elements
        - Visual appeal and stopping power for social media
        - Alignment with the Search trend: {target_search_trends}
        - Platform optimization (aspect ratios, duration)
        - Prompts are maximizing descriptive possibilities to match the intended tone
        - Descriptions of scenes, characters, tone, emotion are all extremely verbose (100+ words) and leverage ideas from the prompting best practices
    </INSTRUCTIONS>

    <CONSTRAINTS>
    **Strict requirement(s):**
    1. Ensure each visual concept markets the target product: {target_product}
    </CONSTRAINTS>
    
    <OUTPUT_FORMAT>
    The output format for each visual concept must include the following:
    -   Name (intuitive name of the concept)
    -   Creative concept explanation
    -   How each concept relates to the Search trend: {target_search_trends}
    -   How each concept markets the target product: {target_product}
    -   A prompt for image generation
    </OUTPUT_FORMAT>
    """,
    # tools=[google_search],
    generate_content_config=types.GenerateContentConfig(temperature=0.7),
    output_key="visual_concept_critique",
)

# CONSTRAINTS
#     2. Explain how each concept relates to the Search trend: {target_search_trends}
# OUTPUT_FORMAT
# -   Detailed rationale explaining why this concept will perform well 

visual_concept_finalizer = Agent(
    model=config.worker_model,
    name="visual_concept_finalizer",
    description="Finalize visual concepts to proceed with.",
    # planner=BuiltInPlanner(thinking_config=types.ThinkingConfig(include_thoughts=True)),
    instruction="""You are a senior creative director finalizing visual concepts for ad creatives.

    Your task is to select the 5 best visual concepts for ad media generation.

    <CONTEXT>
        <visual_concept_critique>
        {visual_concept_critique}
        </visual_concept_critique>
    </CONTEXT>

    <INSTRUCTIONS>
    To complete the task, you need to follow these steps:
    1. Review the critiqued visual concept drafts in the <CONTEXT> block and select the 5 best concepts for ad generation.
    2. For each visual concept, provide the following:
        -   Name (intuitive name of the visual concept)
        -   The trend(s) referenced by this creative.
        -   Headline (attention-grabbing)
        -   A candidate social media caption
        -   Creative concept explanation
        -   How the visual concept relates to the search trend: {target_search_trends}.
        -   Brief rationale for target audience appeal
        -   Brief explanation of how this markets the target product: {target_product}
        -   Brief rationale explaining why this visual concept will perform well.
        -   The prompt for image generation
    </INSTRUCTIONS>
    """,
    # tools=[save_select_visual_concept],
    generate_content_config=types.GenerateContentConfig(temperature=0.8),
    output_key="final_visual_concepts",
)


# Sequential agent for visual generation
visual_generation_pipeline = SequentialAgent(
    name="visual_generation_pipeline",
    description="Generates visual concepts with an actor-critic workflow.",
    sub_agents=[
        visual_concept_drafter,
        visual_concept_critic,
        visual_concept_finalizer,
    ],
)


visual_generator = Agent(
    model=config.critic_model,
    name="visual_generator",
    description="Generate final visuals using image generation tools",
    instruction="""You are a visual content producer generating image creatives.
    Your job is to invoke the `generate_image` tool.
    """,
    tools=[generate_image],
    generate_content_config=types.GenerateContentConfig(temperature=1.2),
    before_model_callback=callbacks.rate_limit_callback,
)


# Main orchestrator agent
root_agent = Agent(
    model=config.lite_planner_model,
    name="root_agent",
    description="Help with ad generation; brainstorm and refine ad copy and visual concept ideas with actor-critic workflows; generate final ad creatives.",
    instruction="""**Role:** You are the orchestrator for a comprehensive ad content generation workflow.

    **Objective:** Your goal is to generate a complete set of ad creatives including ad copy and images. To achieve this, use the <AVAILABLE_TOOLS/> available to complete the <INSTRUCTIONS/> below.
    
    <AVAILABLE_TOOLS>
    1. Use the `memorize` tool to store trends and campaign metadata in the session state.
    2. Use the `combined_research_pipeline` tool to conduct web research on the campaign metadata and selected trends.
    3. Use the `save_draft_report_artifact` tool to save a research PDf report to Cloud Storage.
    4. Use the `ad_creative_pipeline` tool to generate ad copies.
    5. Use the `save_select_ad_copy` tool to update the 'final_select_ad_copies' state key with the final ad copies generated with the `ad_creative_pipeline` tool.
    6. Use the `visual_generation_pipeline` tool to create visual concepts for each ad copy.
    7. Use the `save_select_visual_concept` tool to update the 'final_select_vis_concepts' state key with the final visual concepts generated with the `visual_generation_pipeline` tool.
    8. Use the `visual_generator` tool to generate image creatives.
    9. Use the `save_creatives_html_report` tool to build the final HTML report, detailing research and creatives generated during a session.
    10. Use the `save_creative_gallery_html` tool to build an HTML file for displaying a portfolio of the generated creatives generated during the session.
    11. Use the `save_session_state_to_gcs` tool at the end of the session to save the state dict to Cloud Storage.
    </AVAILABLE_TOOLS>

    <INSTRUCTIONS>
    1. First, complete the following information if any is blank:
        <brand>{brand}</brand>
        <target_audience>{target_audience}</target_audience>
        <target_product>{target_product}</target_product>
        <key_selling_points>{key_selling_points}</key_selling_points>
        <target_search_trends>{target_search_trends}</target_search_trends>
    2. Use the `memorize` tool to store campaign metadata and search trends into the following variables:
        - `brand`, 
        - `target_audience`
        - `target_product` 
        - `key_selling_points` and
        - `target_search_trends`
        To make sure everything is stored correctly, instead of calling memorize all at once, chain the calls such that 
        you only call another `memorize` after the last call has responded. 
    3. Then, complete all steps in the <WORKFLOW/> block to generate ad creatives. Strictly follow all the steps one-by-one.
    </INSTRUCTIONS>

    <WORKFLOW>
    1. First, use the `combined_research_pipeline` tool to conduct web research on the campaign metadata and selected trends.
    2. Once all research tasks are complete, use the `save_draft_report_artifact` tool to save the research as a markdown file in Cloud Storage.
    3. Invoke the `ad_creative_pipeline` tool to generate a set of candidate ad copies.
    4. Once the previous step completes, use the `save_select_ad_copy` tool to save each finalized ad copy idea to the `final_select_ad_copies` state key.
        -   To make sure everything is stored correctly, instead of calling `save_select_ad_copy` all at once, chain the calls such that you only call another `save_select_ad_copy` after the last call has responded.
        -   Once these complete, proceed to the next step.
    5. Then, call the `visual_generation_pipeline` tool to generate visual concepts.
    6. Once the previous step completes, use the `save_select_visual_concept` tool to save each finalized visual concept to the `final_visual_concepts` state key.
        -   To make sure everything is stored correctly, instead of calling `save_select_visual_concept` all at once, chain the calls such that you only call another `save_select_visual_concept` after the last call has responded.
        -   Once these complete, proceed to the next step.
    7. Next, call the `visual_generator` tool to generate ad creatives.
    8. After the previous step is complete, use the `save_creatives_html_report` tool to create the final HTML report and save it to Cloud Storage. 
    9. Next, call the `save_creative_gallery_html` tool to create an HTML portfolio and save it to Cloud Storage.
    10. Finally as the last step, call the `save_session_state_to_gcs` tool to save the session state to Cloud Storage.

    Once the previous steps are complete, perform the following action:

    Action 1: Display Cloud Storage location to the user
    Display the Cloud Storage URI to the user by combining the 'gcs_bucket', 'gcs_folder', and 'agent_output_dir' state keys like this: {gcs_bucket}/{gcs_folder}/{agent_output_dir}

        <EXAMPLE>
            INPUT: {gcs_bucket}/{gcs_folder}/{agent_output_dir}

            OUTPUT: gs://trend-trawler-deploy-ae/2025_09_13_19_21/creative_output
        </EXAMPLE>
    </WORKFLOW>
    
    Your job is complete when all tasks in the <WORKFLOW> block are complete.
    """,
    tools=[
        AgentTool(agent=combined_research_pipeline),
        AgentTool(agent=ad_creative_pipeline),
        AgentTool(agent=visual_generation_pipeline),
        AgentTool(agent=visual_generator),
        save_draft_report_artifact,
        save_creatives_html_report,
        save_select_visual_concept,
        save_select_ad_copy,
        save_creative_gallery_html,
        save_session_state_to_gcs,
        memorize,
    ],
    generate_content_config=types.GenerateContentConfig(
        temperature=1.0, labels={"agent": "trend_trawler"}
    ),
    before_agent_callback=callbacks._load_session_state,
    before_model_callback=callbacks.rate_limit_callback,
)
