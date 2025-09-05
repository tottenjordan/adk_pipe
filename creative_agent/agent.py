import datetime
import logging

logging.basicConfig(level=logging.INFO)

from pydantic import BaseModel, Field

from google.genai import types
from google.adk.tools import google_search
from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.agents import Agent, SequentialAgent, ParallelAgent

from . import callbacks
from .config import config
from .prompts import VEO3_INSTR
from .sub_agents.campaign_researcher.agent import ca_sequential_planner
from .sub_agents.trend_researcher.agent import gs_sequential_planner
from .tools import (
    memorize,
    generate_image,
    generate_video,
    save_img_artifact_key,
    save_vid_artifact_key,
    save_select_ad_copy,
    save_select_visual_concept,
    save_creatives_html_report,
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
    Structure your response using headings for each topic. Ensure the report is coherent and integrates the key points smoothly.

    ---
    **Output Format:**

    # Summary of Campaign and Trend Research

    ## Campaign Guide
    {campaign_web_search_insights}

    ## Search Trend
    {gs_web_search_insights}

    Output *only* the structured report following this format. Do not include introductory or concluding phrases outside this structure, and strictly adhere to using only the provided input summary content.
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
    Transform the provided data into a polished, professional, and meticulously cited research report.

    ---
    **INPUT DATA**

    *   **Search Trends:**
        {target_search_trends}
    
    *   **Final Research:**
        {combined_web_search_insights}
    
    *   **Citation Sources:** 
        `{sources}`

    ---
    **CRITICAL: Citation System**
    To cite a source, you MUST insert a special citation tag directly after the claim it supports.

    **The only correct format is:** `<cite source="src-ID_NUMBER" />`

    ---
    **OUTPUT FORMAT**
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

    ---
    **Final Instructions**
    Generate a comprehensive report using ONLY the `<cite source="src-ID_NUMBER" />` tag system for all citations.
    Ensure the final report follows a structure similar to the one proposed in the **OUTPUT FORMAT**
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

    Your goal is to review the research and trends provided in the **Input Data** to generate 10 culturally relevant ad copy ideas.
 
    ---
    ### Input Data

    <target_search_trends>
    {target_search_trends}
    </target_search_trends>
    
    <combined_final_cited_report>
    {combined_final_cited_report}
    </combined_final_cited_report>

    ---
    ### Instructions

    1. Review the campaign and trend research in the 'combined_final_cited_report' state key.
    2. Using insights related to the campaign metadata and trending Search term(s), generate 10 diverse ad copy ideas that:
        - Incorporate key selling points for the {target_product}
        - Vary in tone, style, and approach
        - Are suitable for Instagram/TikTok platforms
        - Reference the trending Search term: {target_search_trends}.
    3. **Each ad copy should include:**
        - Headline (attention-grabbing)
        - Body text (concise and compelling)
        - Call-to-action
        - How it relates to the trending topic (i.e., {target_search_trends})
        - Brief rationale for target audience appeal
        - A candidate social media caption

    Use the `google_search` tool to support your decisions.

    """,
    generate_content_config=types.GenerateContentConfig(
        temperature=1.5,
    ),
    tools=[google_search],
    output_key="ad_copy_draft",
)


ad_copy_critic = Agent(
    model=config.critic_model,
    name="ad_copy_critic",
    description="Critique and narrow down ad copies based on product, audience, and trends",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""You are a strategic marketing critic evaluating ad copy ideas.

    Your goal is to review the proposed candidates in the 'ad_copy_draft' state key and select the 5 BEST ad copies based on:
    1. Alignment with target audience.
    2. Effective use of trending topic that feels authentic.
    3. Clear communication of key selling points.
    4. Platform-appropriate tone and length.

    Use the `google_search` tool to support your decisions
    
    Provide detailed rationale for your selections, explaining why these specific copies will perform best.
    
    Each ad copy should include:
    - Headline (attention-grabbing)
    - Call-to-action
    - A candidate social media caption
    - Body text (concise and compelling)
    - How it relates to the trending topic (i.e., {target_search_trends})
    - Brief rationale for target audience appeal
    - Detailed rationale explaining why this ad copy will perform well

    """,
    tools=[google_search],
    generate_content_config=types.GenerateContentConfig(temperature=0.7),
    output_key="ad_copy_critique",
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


# --- PROMPT GENERATION SUBAGENTS ---
visual_concept_drafter = Agent(
    model=config.worker_model,
    name="visual_concept_drafter",
    description="Generate initial visual concepts for selected ad copies",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=f"""You are a visual creative director generating initial concepts and an expert at creating AI prompts for {config.image_gen_model} and {config.video_gen_model}.
    
    Based on the selected ad copies in the 'ad_copy_critique' state key, generate visual concepts that follow these criteria:
    - Incorporate trending visual styles and themes.
    - Consider platform-specific best practices.
    - Find a clever way to market the 'target_product'.
    - References the trend from the 'target_search_trends' state key.

    Generate at least one image concept for each ad copy idea.

    For each visual concept, provide:
    -   Name (intuitive name of the concept)
    -   Type (image or video)
    -   Which trend(s) it relates to (e.g., 'target_search_trends' state key)
    -   Which ad copy it connects to
    -   Creative concept explanation
    -   A draft {config.image_gen_model} or {config.video_gen_model} prompt.
    -   If this is a video concept:
        -   Consider generated videos are 8 seconds in length.
        -   Consider the prompting best practices in the <PROMPTING_BEST_PRACTICES/> block.

    Use the `google_search` tool to support your decisions.

    <PROMPTING_BEST_PRACTICES>
    {VEO3_INSTR}
    </PROMPTING_BEST_PRACTICES>
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
    instruction=f"""You are a creative director evaluating visual concepts and high quality prompts that result in high impact.
    
    Review the concepts in the 'visual_draft' state key and critique the draft prompts on:
    1. Visual appeal and stopping power for social media
    2. Alignment with ad copy messaging
    3. Alignment with trend (i.e., see the 'target_search_trends' state key)
    4. Platform optimization (aspect ratios, duration)
    5. Diversity of visual approaches
    6. Utilize techniques to maintain continuity in the prompts
    7. Prompts are maximizing descriptive possibilities to match the intended tone
    8. Descriptions of scenes, characters, tone, emotion are all extremely verbose (100+ words) and leverage ideas from the prompting best practices
    9. These verbose descriptions are maintained scene to scene to avoid saying things like "the same person", instead use the same provided description

    **Critical Guidelines**
    * Ensure each visual concept markets the target product
    * Explain how each concept relates to the search trend in the 'target_search_trends' state key.
    * Provide detailed rationale for your selections.
    * Consider the prompting best practices in the <PROMPTING_BEST_PRACTICES/> block.
    * Use the `google_search` tool to support your decisions.

    **Final Output:**
    Format the final output to include the following information for each visual concept:
    -   Name (intuitive name of the concept)
    -   Type (image or video)
    -   How each concept relates to the search trend in the 'target_search_trends' state key.
    -   How each concept markets the target product
    -   Creative concept explanation
    -   Detailed rationale explaining why this concept will perform well 
    -   A draft Imagen or Veo prompt

    <PROMPTING_BEST_PRACTICES>
    {VEO3_INSTR}
    </PROMPTING_BEST_PRACTICES>
    """,
    tools=[google_search],
    generate_content_config=types.GenerateContentConfig(temperature=0.7),
    output_key="visual_concept_critique",
)
# * Ensure a good mix of images and videos in your selections.

visual_concept_finalizer = Agent(
    model=config.worker_model,
    name="visual_concept_finalizer",
    description="Finalize visual concepts to proceed with.",
    # planner=BuiltInPlanner(thinking_config=types.ThinkingConfig(include_thoughts=True)),
    instruction="""You are a senior creative director finalizing visual concepts for ad creatives.

    1. Review the 'visual_concept_critique' state key to understand the refined visual concepts.
    2. For each concept, provide the following:
        -   Name (intuitive name of the concept)
        -   Type (image or video)
        -   Headline (attention-grabbing)
        -   How each concept relates to the search trend: {target_search_trends}.
        -   Call-to-action
        -   A candidate social media caption
        -   Creative concept explanation
        -   Brief rationale for target audience appeal
        -   Brief explanation of how this markets the target product
        -   A draft Imagen or Veo prompt.
    
    """,
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
    description="Generate final visuals using image and video generation tools",
    instruction=f"""You are a visual content producer creating final assets.
    
    **Objective:** Generate visual content options (images and videos) based on the selected visual concepts.

    **Available Tools:**
    - `generate_image`:tool to generate images using Google's Imagen model.
    - `generate_video`: tool to generate videos using Google's Veo model.

    **Instructions:**
    1. For each selected visual concept in the 'final_visual_concepts' state key, generate the creative visual using the appropriate tool (`generate_image` or `generate_video`).
        - For images, follow the instructions in the <IMAGE_GENERATION/> block, 
        - For videos, follow the instructions in the <VIDEO_GENERATION/> block and consider prompting best practices in the <PROMPTING_BEST_PRACTICES/> block,

    <IMAGE_GENERATION>
    - Create descriptive image prompts that visualize the ad copy concepts
    - Include subject, context/background, and style elements
    - Ensure prompts capture the essence of the trends and campaign highlights
    - Generate diverse visual approaches (different styles, compositions, contexts)
    </IMAGE_GENERATION>

    <VIDEO_GENERATION>
    - Create dynamic video prompts that bring the ad copy to life
    - Include subject, context, action, style, and optional camera/composition elements
    - Consider continuity with the image concepts when appropriate
    - Vary the approaches (different actions, camera angles, moods)
    </VIDEO_GENERATION>

    <PROMPTING_BEST_PRACTICES>
     {VEO3_INSTR}
    </PROMPTING_BEST_PRACTICES>
    """,
    tools=[
        generate_image,
        generate_video,
        # save_img_artifact_key,
        # save_vid_artifact_key,
    ],
    generate_content_config=types.GenerateContentConfig(temperature=1.2),
    before_model_callback=callbacks.rate_limit_callback,
)


# Main orchestrator agent
root_agent = Agent(
    model=config.lite_planner_model,
    name="root_agent",
    description="Help with ad generation; brainstorm and refine ad copy and visual concept ideas with actor-critic workflows; generate final ad creatives.",
    instruction="""**Role:** You are the orchestrator for a comprehensive ad content generation workflow.

    **Objective:** Your goal is to generate a complete set of ad creatives including ad copy, images, and videos. To achieve this, use the **specialized tools and sub-agents** available to complete the **instructions** below.
    
    **You have access to specialized tools and sub-agents:**
    1. Use the `combined_research_pipeline` tool to conduct web research on the campaign metadata and selected trends.
    2. Use the `ad_creative_pipeline` tool to generate ad copies.
    3. Use the `visual_generation_pipeline` tool to create visual concepts for each ad copy.
    5. Use the `visual_generator` tool to generate image and video creatives.
    6. Use the `memorize` tool to store trends and campaign metadata in the session state.
    7. Use the `save_creatives_html_report` tool to build the final HTML report, detailing research and creatives generated during a session.
    8. Use the `save_img_artifact_key` tool to update the 'img_artifact_keys' state key for each image generated with the `generate_image` tool.
    9. Use the `save_vid_artifact_key` tool to update the 'vid_artifact_keys' state key for each video generated with the `generate_video` tool.

    **Instructions:**
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

    <WORKFLOW>
    1. Call the `combined_research_pipeline` tool to conduct web research on the campaign metadata and selected search trend.
    2. Then, call `ad_creative_pipeline` as a tool to generate a set of candidate ad copies.
    3. Next, call the `visual_generation_pipeline` tool to generate visual concepts for each ad copy.
    4. Next, call the `visual_generator` tool to generate ad creatives from the selected visual concepts.
        -   For each image generated, call the `save_img_artifact_key` tool to update the 'img_artifact_keys' state key.
        -   For each video generated, call the `save_vid_artifact_key` tool to update the 'vid_artifact_keys' state key. 
    5. Finally, after the previous step is complete, use the `save_creatives_html_report` tool to create the final HTML report and save it to Cloud Storage
    </WORKFLOW>
    
    """,
    tools=[
        AgentTool(agent=combined_research_pipeline),
        AgentTool(agent=ad_creative_pipeline),
        AgentTool(agent=visual_generation_pipeline),
        AgentTool(agent=visual_generator),
        save_img_artifact_key,
        save_vid_artifact_key,
        # save_select_ad_copy,
        # save_select_visual_concept,
        memorize,
        save_creatives_html_report,
        # load_artifacts,
    ],
    generate_content_config=types.GenerateContentConfig(temperature=1.0),
    before_agent_callback=callbacks._load_session_state,
    before_model_callback=callbacks.rate_limit_callback,
)

# 5. Once all visuals are created, call the following tools to update the session state keys:
# 10. Use the `save_select_visual_concept` tool to save the finalized visual concepts to the session state.

# 3. Once the previous step is complete, use the `save_select_ad_copy` tool to add the critiqued ad copies to the session state.
#     -   To make sure everything is stored correctly, instead of calling `save_select_ad_copy` all at once, chain the calls such that you only call another `save_select_ad_copy` after the last call has responded.
#     -   Once these complete, proceed to the next step.
# 4. Once the previous step is complete, use the `save_select_visual_concept` tool to add the finalized visual concepts to the session state.
#     -   To make sure everything is stored correctly, instead of calling `save_select_visual_concept` all at once, chain the calls such that you only call another `save_select_visual_concept` after the last call has responded.
#     -   Once these complete, proceed to the next step.

# Chain the calls such that you only call another `save_img_artifact_key` after the last call has responded.
# Chain the calls such that you only call another `save_vid_artifact_key` after the last call has responded.