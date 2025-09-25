import os, datetime
import logging

logging.basicConfig(level=logging.INFO)

from google.genai import types
from pydantic import BaseModel
from google.adk.tools import google_search
from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.agents import Agent, SequentialAgent

from . import callbacks
from .config import config
from .prompts import VEO3_INSTR
from .tools import (
    memorize,
    generate_image,
    generate_video,
    save_img_artifact_key,
    save_vid_artifact_key,
    save_creatives_html_report,
)

# --- AD CREATIVE SUBAGENTS ---
ad_copy_drafter = Agent(
    model=config.worker_model,
    name="ad_copy_drafter",
    description="Generate 10-12 initial ad copy ideas based on campaign guidelines and trends",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""You are a creative copywriter generating initial ad copy ideas.

    Your task is to review the research and trend provided in the <CONTEXT> block and generate 10 culturally relevant ad copy ideas.

    <CONTEXT>
        <target_search_trends>
        {target_search_trends}
        </target_search_trends>
        
        <combined_final_cited_report>
        {combined_final_cited_report}
        </combined_final_cited_report>
    </CONTEXT>
    
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
        - Call-to-action
        - How it relates to the trending topic: {target_search_trends}
        - Brief rationale for target audience appeal
        - A candidate social media caption
    </INSTRUCTIONS>

    <CONSTRAINTS>
    Dos and don'ts for the following aspects
    1. Use the `google_search` tool to support your decisions.
    </CONSTRAINTS>
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

    Your task is to review candidate ad copies and select the 5 BEST ideas

    <INSTRUCTIONS>
    To complete the task, you need to follow these steps:
    1. Review the proposed candidates in the 'ad_copy_draft' state key.
    2. Select the 5 best ad copy ideas based on the following criteria:
        - Alignment with target audience.
        - Effective use of trending topic that feels authentic.
        - Clear communication of key selling points.
        - Platform-appropriate tone and length.
    3. Provide detailed rationale for your selections, explaining why these specific copies will perform best.
    </INSTRUCTIONS>

    <CONSTRAINTS>
    Dos and don'ts for the following aspects
    1. Use the `google_search` tool to support your decisions.
    </CONSTRAINTS>
    
    <OUTPUT_FORMAT>
    Each ad copy should include:
        - Headline (attention-grabbing)
        - Call-to-action
        - A candidate social media caption
        - Body text (concise and compelling)
        - How it relates to the trending topic: {target_search_trends}
        - Brief rationale for target audience appeal
        - Detailed rationale explaining why this ad copy will perform well
    </OUTPUT_FORMAT>

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

# TODO: need to add these: (f string)
VEO_F_STRING=f"""
    <PROMPTING_BEST_PRACTICES>
    {VEO3_INSTR}
    </PROMPTING_BEST_PRACTICES>
"""

visual_concept_drafter = Agent(
    model=config.worker_model,
    name="visual_concept_drafter",
    description="Generate initial visual concepts for selected ad copies",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""You are a visual creative director generating initial concepts and an expert at creating AI prompts for Imagen and Veo.

    Your task is to review the ad copies in the <CONTEXT> block and generate culturally relevant visual concepts.

    Use the `google_search` tool to support your decisions.

    <INSTRUCTIONS>
    To complete the task, you need to follow these steps:
    1. Based on the selected ad copies in the <CONTEXT> block, generate visual concepts that follow these criteria:
        - Incorporate trending visual styles and themes.
        - Consider platform-specific best practices.
        - Find a clever way to market the target product: {target_product}
        - References the Search trend: {target_search_trends}
    2. Generate at least one image concept for each ad copy idea.
    </INSTRUCTIONS>

    <CONTEXT>
        <ad_copy_critique>
        {ad_copy_critique}
        </ad_copy_critique>
    </CONTEXT>

    <OUTPUT_FORMAT>
    For each visual concept, provide:
    -   Name (intuitive name of the concept)
    -   Type (image or video)
    -   How it relates to the Search trend: {target_search_trends}
    -   Which ad copy it connects to
    -   Creative concept explanation
    -   A draft Imagen or Veo prompt.
    -   If this is a video concept:
        -   Consider generated videos are 8 seconds in length.
        -   Consider the prompting best practices in the <PROMPTING_BEST_PRACTICES/> block.
    </OUTPUT_FORMAT>

    """ + VEO_F_STRING,
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
    instruction="""You are a creative director evaluating visual concepts and high quality prompts that result in high impact.

    Your task is to critique the image prompts in the proposed visual concept drafts

    <INSTRUCTIONS>
    To complete the task, you need to follow these steps:
    1. Review the visual concept drafts in the <CONTEXT> block and critique the draft Imagen or Veo prompts on:
        - Visual appeal and stopping power for social media
        - Alignment with ad copy messaging
        - Alignment with trend (i.e., see the 'target_search_trends' state key)
        - Platform optimization (aspect ratios, duration)
        - Diversity of visual approaches
        - Utilize techniques to maintain continuity in the prompts
        - Prompts are maximizing descriptive possibilities to match the intended tone
        - Descriptions of scenes, characters, tone, emotion are all extremely verbose (100+ words) and leverage ideas from the prompting best practices
    </INSTRUCTIONS>
    
    <CONTEXT>
        <visual_draft>
        {visual_draft}
        </visual_draft>
    </CONTEXT>

    <CONSTRAINTS>
    Dos and don'ts for the following aspects
    1. Ensure each visual concept markets the target product
    2. Explain how each concept relates to the Search trend: {target_search_trends}
    3. Provide detailed rationale for your selections.
    4. Consider the prompting best practices in the <PROMPTING_BEST_PRACTICES/> block.
    5. Use the `google_search` tool as needed to support your decisions.
    </CONSTRAINTS>
    
    <OUTPUT_FORMAT>
    The output format for each visual concept must include the following:
    -   Name (intuitive name of the concept)
    -   Type (image or video)
    -   How each concept relates to the Search trend: {target_search_trends}
    -   How each concept markets the target product: {target_product}
    -   Creative concept explanation
    -   Detailed rationale explaining why this concept will perform well 
    -   A draft Imagen or Veo prompt
    </OUTPUT_FORMAT>

    """ + VEO_F_STRING,
    tools=[google_search],
    generate_content_config=types.GenerateContentConfig(temperature=0.7),
    output_key="visual_concept_critique",
)


visual_concept_finalizer = Agent(
    model=config.worker_model,
    name="visual_concept_finalizer",
    description="Finalize visual concepts to proceed with.",
    # planner=BuiltInPlanner(thinking_config=types.ThinkingConfig(include_thoughts=True)),
    instruction="""You are a senior creative director finalizing visual concepts for ad creatives.

    Your task is to present the finalized visual concepts for ad media generation.

    <INSTRUCTIONS>
    To complete the task, you need to follow these steps:
    1. Review the critiqued visual concept drafts in the <CONTEXT> block.
    2. For each concept, provide the following:
        -   Name (intuitive name of the concept)
        -   Type (image or video)
        -   Headline (attention-grabbing)
        -   How each concept relates to the search trend: {target_search_trends}.
        -   Call-to-action
        -   A candidate social media caption
        -   Creative concept explanation
        -   Brief rationale for target audience appeal
        -   Brief explanation of how this markets the target product: {target_product}
        -   A draft Imagen or Veo prompt.
    </INSTRUCTIONS>

    <CONTEXT>
        <visual_concept_critique>
        {visual_concept_critique}
        </visual_concept_critique>
    </CONTEXT>
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
    
    Your objective is to generate visual content options (images and videos) based on the selected visual concepts.

    **Available Tools:**
    - `generate_image`:tool to generate images using Google's Imagen model.
    - `generate_video`: tool to generate videos using Google's Veo model.

    <INSTRUCTIONS>
    1. For each selected visual concept in the 'final_visual_concepts' state key, generate the creative visual using the appropriate tool (`generate_image` or `generate_video`).
        - For images, follow the instructions in the <IMAGE_GENERATION/> block, 
        - For videos, follow the instructions in the <VIDEO_GENERATION/> block and consider prompting best practices in the <PROMPTING_BEST_PRACTICES/> block,
    </INSTRUCTIONS>

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
    1. Use the `ad_creative_pipeline` tool to generate ad copies.
    2. Use the `visual_generation_pipeline` tool to create visual concepts for each ad copy.
    3. Use the `visual_generator` tool to generate image and video creatives.
    4. Use the `memorize` tool to store trends and campaign metadata in the session state.
    5. Use the `save_creatives_html_report` tool to build the final HTML report, detailing research and creatives generated during a session.
    6. Use the `save_img_artifact_key` tool to update the 'img_artifact_keys' state key for each image generated with the `generate_image` tool.
    7. Use the `save_vid_artifact_key` tool to update the 'vid_artifact_keys' state key for each video generated with the `generate_video` tool.

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
    1. Invoke the `ad_creative_pipeline` tool to generate a set of candidate ad copies.
    2. Then, call the `visual_generation_pipeline` tool to generate visual concepts for each ad copy.
    3. Next, call the `visual_generator` tool to generate ad creatives from the selected visual concepts.
        -   For each image generated, call the `save_img_artifact_key` tool to update the 'img_artifact_keys' state key.
        -   For each video generated, call the `save_vid_artifact_key` tool to update the 'vid_artifact_keys' state key. 
    4. Finally, after the previous step is complete, use the `save_creatives_html_report` tool to create the final HTML report and save it to Cloud Storage
    </WORKFLOW>
    
    Your job is complete when all tasks in the <WORKFLOW> block are complete.
    """,
    tools=[
        AgentTool(agent=ad_creative_pipeline),
        AgentTool(agent=visual_generation_pipeline),
        AgentTool(agent=visual_generator),
        save_img_artifact_key,
        save_vid_artifact_key,
        memorize,
        save_creatives_html_report,
        # load_artifacts,
    ],
    generate_content_config=types.GenerateContentConfig(temperature=1.0),
    before_agent_callback=callbacks._load_session_state,
    before_model_callback=callbacks.rate_limit_callback,
)