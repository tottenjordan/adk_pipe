import os, datetime, logging

logging.basicConfig(level=logging.INFO)

from google.genai import types
from pydantic import BaseModel, Field
from google.adk.tools import google_search
from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.agents import Agent, SequentialAgent, ParallelAgent

from . import callbacks
from .config import config
from .sub_agents.campaign_researcher.agent import ca_sequential_planner
from .sub_agents.trend_researcher.agent import gs_sequential_planner
from .tools import write_to_file, save_session_state_to_gcs, memorize


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

# Main orchestrator agent
research_orchestrator = Agent(
    model=config.worker_model,
    name="research_orchestrator",
    description="Orchestrate comprehensive research for the campaign metadata and trending topics.",
    instruction="""You are the orchestrator for a comprehensive research workflow.
    Your task is to facilitate several research tasks and produce a research report.

    Once initiated, immediately begin the steps below.

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
        - `key_selling_points`
        - `target_search_trends`
        To make sure everything is stored correctly, instead of calling memorize all at once, chain the calls such that 
        you only call another `memorize` after the last call has responded. 
    3. Then, complete all steps in the <WORKFLOW/> block. Strictly follow all the steps one-by-one.

    <WORKFLOW>
    1. First, use the `combined_research_pipeline` tool (agent tool) to conduct web research on the campaign metadata and selected trends.
    2. Once all research tasks are complete, use the `write_to_file` tool to save the research as a markdown file in Cloud Storage.
    3. Call the `save_session_state_to_gcs` tool to save the session state to Cloud Storage.
    4. Once the previous steps are complete, perform the following action:
    
    Action 1: Display Cloud Storage URI to user 
    Display the Cloud Storage URI to the user by combining the 'gcs_bucket', 'gcs_folder', and 'agent_output_dir' state keys like this: {gcs_bucket}/{gcs_folder}/{agent_output_dir}

        <EXAMPLE>
            INPUT: {gcs_bucket}/{gcs_folder}/{agent_output_dir}

            OUTPUT: gs://trend-trawler-deploy-ae/2025_09_13_19_21/research_output
        </EXAMPLE>
    
    </WORKFLOW>

    Your job is complete when all tasks in the <WORKFLOW> block are complete.
    """,
    tools=[
        memorize,
        write_to_file,
        save_session_state_to_gcs,
        AgentTool(agent=combined_research_pipeline),
    ],
    generate_content_config=types.GenerateContentConfig(temperature=1.0),
    before_agent_callback=[
        callbacks._load_session_state,
    ],
    before_model_callback=callbacks.rate_limit_callback,
)

# Set as root agent
root_agent = research_orchestrator
