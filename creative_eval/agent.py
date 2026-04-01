"""ADK agent integration for creative evaluation.

Provides an evaluation agent that can be plugged into the creative_agent
pipeline as an additional step after visual generation.

Usage:
    from creative_eval.agent import creative_eval_agent

    # Add to root_agent tools:
    tools=[..., AgentTool(agent=creative_eval_agent)]
"""

import json
import logging
from google.adk.agents import Agent
from google.genai import types

from .config import EvalConfig
from .evaluate import evaluate_ad_copy, evaluate_visual_concept, _build_summary
from .schemas import CreativeEvaluationReport

logger = logging.getLogger(__name__)

_config = EvalConfig()


def evaluate_all_creatives(tool_context) -> dict:
    """Evaluate all finalized ad copies and visual concepts in session state.

    Reads from state keys:
      - ad_copy_critique (FinalAdCopyList JSON)
      - final_visual_concepts (VisualConceptFinalList JSON)
      - brand, target_product, target_audience, key_selling_points, target_search_trends

    Writes to state key:
      - creative_evaluation_report (CreativeEvaluationReport JSON)

    Returns:
        Summary dict with pass rates and weakest dimensions.
    """
    state = tool_context.state

    # Extract campaign context from state
    campaign_context = {
        "brand": state.get("brand", ""),
        "target_product": state.get("target_product", ""),
        "target_audience": state.get("target_audience", ""),
        "key_selling_points": state.get("key_selling_points", ""),
        "target_search_trend": state.get("target_search_trends", ""),
    }

    # Parse ad copies from state
    ad_copy_raw = state.get("ad_copy_critique", {})
    if isinstance(ad_copy_raw, str):
        ad_copy_raw = json.loads(ad_copy_raw)
    ad_copies = ad_copy_raw.get("ad_copies", []) if isinstance(ad_copy_raw, dict) else []

    # Parse visual concepts from state
    visual_raw = state.get("final_visual_concepts", {})
    if isinstance(visual_raw, str):
        visual_raw = json.loads(visual_raw)
    visual_concepts = visual_raw.get("visual_concepts", []) if isinstance(visual_raw, dict) else []

    if not ad_copies and not visual_concepts:
        return {
            "status": "error",
            "message": "No ad copies or visual concepts found in session state to evaluate.",
        }

    logger.info(
        f"Evaluating {len(ad_copies)} ad copies and {len(visual_concepts)} visual concepts..."
    )

    # Evaluate ad copies
    ad_evals = []
    for ac in ad_copies:
        if isinstance(ac, str):
            ac = json.loads(ac)
        ad_evals.append(evaluate_ad_copy(ac, campaign_context, _config))

    # Evaluate visual concepts
    visual_evals = []
    for vc in visual_concepts:
        if isinstance(vc, str):
            vc = json.loads(vc)
        visual_evals.append(evaluate_visual_concept(vc, campaign_context, _config))

    summary = _build_summary(ad_evals, visual_evals)

    report = CreativeEvaluationReport(
        brand=campaign_context["brand"],
        target_product=campaign_context["target_product"],
        target_search_trend=campaign_context["target_search_trend"],
        ad_copy_evaluations=ad_evals,
        visual_concept_evaluations=visual_evals,
        summary=summary,
    )

    # Store in session state
    state["creative_evaluation_report"] = report.model_dump()

    return {
        "status": "success",
        "total_ad_copies": summary.total_ad_copies,
        "ad_copies_passed": summary.ad_copies_passed,
        "avg_ad_copy_score": summary.avg_ad_copy_score,
        "total_visual_concepts": summary.total_visual_concepts,
        "visual_concepts_passed": summary.visual_concepts_passed,
        "avg_visual_score": summary.avg_visual_score,
        "overall_pass_rate": summary.overall_pass_rate,
        "weakest_dimensions": summary.weakest_dimensions,
    }


# ADK Agent that wraps the evaluation tool
creative_eval_agent = Agent(
    model=_config.eval_model,
    name="creative_eval_agent",
    include_contents="none",
    description="Evaluate the quality of generated ad copies and visual concepts using LLM-as-judge scoring.",
    instruction="""Role: You are a Creative Quality Evaluation Specialist.

    Your task is to evaluate all finalized ad copies and visual concepts using the `evaluate_all_creatives` tool.

    <INSTRUCTIONS>
    1. Call the `evaluate_all_creatives` tool to score all creatives in the session.
    2. Report the results: overall pass rate, average scores, and weakest dimensions.
    3. Highlight any creatives that failed (score < 0.7) and explain why.
    </INSTRUCTIONS>

    Call the tool now and report the results.
    """,
    tools=[evaluate_all_creatives],
)
