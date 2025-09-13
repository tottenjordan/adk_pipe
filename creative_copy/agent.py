import os, datetime
import logging

logging.basicConfig(level=logging.INFO)

from google.genai import types
from pydantic import BaseModel, Field
from google.adk.tools import google_search
from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.agents import Agent, SequentialAgent, ParallelAgent

from . import callbacks
from .config import config
from .prompts import VEO3_INSTR


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
visual_concept_drafter = Agent(
    model=config.worker_model,
    name="visual_concept_drafter",
    description="Generate initial visual concepts for selected ad copies",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction="""You are a visual creative director generating initial concepts and an expert at creating AI prompts for Imagen and Veo.

    Your task is to review the ad copies in the <CONTEXT> block and generate culturally relevant visual concepts.

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

    <CONSTRAINTS>
    Dos and don'ts for the following aspects
    1. Use the `google_search` tool to support your decisions.
    </CONSTRAINTS>

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
    """,
    tools=[google_search],
    generate_content_config=types.GenerateContentConfig(temperature=1.5),
    output_key="visual_draft",

# TODO: need to add these: (f string)
    # <PROMPTING_BEST_PRACTICES>
    # {VEO3_INSTR}
    # </PROMPTING_BEST_PRACTICES>
)


visual_concept_critic = Agent(
    model=config.critic_model,
    name="visual_concept_critic",
    description="Critique and narrow down visual concepts",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=f"""You are a creative director evaluating visual concepts and high quality prompts that result in high impact.

    Your task is to critique the proposed visual concept drafts
    
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
