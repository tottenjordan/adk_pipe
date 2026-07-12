"""Pydantic schemas for creative evaluation results."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class EvalVerdict(BaseModel):
    """A single rubric verdict — pass/fail on one evaluation dimension."""

    dimension: str = Field(
        description="The evaluation dimension (e.g., 'trend_authenticity', 'copy_quality')."
    )
    score: int = Field(
        description="Score from 1 to 10 for this dimension.",
        ge=1,
        le=10,
    )
    verdict: Literal["pass", "fail"] = Field(
        description="'pass' if score >= 7, 'fail' otherwise."
    )
    rationale: str = Field(
        description="1-2 sentence explanation of the score."
    )


class CreativeScore(BaseModel):
    """Aggregate score for a single creative (ad copy or visual concept)."""

    overall_score: float = Field(
        description="Weighted average score across all dimensions (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )
    passed: bool = Field(
        description="True if overall_score >= passing threshold."
    )
    verdicts: list[EvalVerdict] = Field(
        description="Per-dimension verdicts."
    )
    strengths: list[str] = Field(
        description="Top 2-3 strengths identified."
    )
    improvements: list[str] = Field(
        description="Top 2-3 suggested improvements."
    )


class AdCopyEvaluation(BaseModel):
    """Evaluation result for a single finalized ad copy."""

    original_id: int = Field(description="Maps to FinalAdCopy.original_id.")
    headline: str = Field(description="The headline that was evaluated.")
    tone_style: str = Field(description="The tone/style of this ad copy.")
    score: CreativeScore = Field(description="Scoring details.")


class VisualConceptEvaluation(BaseModel):
    """Evaluation result for a single finalized visual concept."""

    ad_copy_id: int = Field(description="Maps to VisualConceptFinal.ad_copy_id.")
    concept_name: str = Field(description="The concept that was evaluated.")
    score: CreativeScore = Field(description="Scoring details.")


class EvaluationSummary(BaseModel):
    """Aggregate statistics for the evaluation report."""

    total_ad_copies: int = Field(description="Number of ad copies evaluated.")
    ad_copies_passed: int = Field(description="Number of ad copies that passed.")
    avg_ad_copy_score: float = Field(description="Average score across ad copies.")
    total_visual_concepts: int = Field(description="Number of visual concepts evaluated.")
    visual_concepts_passed: int = Field(description="Number of visual concepts that passed.")
    avg_visual_score: float = Field(description="Average score across visual concepts.")
    overall_pass_rate: float = Field(
        description="Combined pass rate across all creatives (0.0-1.0)."
    )
    weakest_dimensions: list[str] = Field(
        description="Dimensions with the lowest average scores across all creatives."
    )


class CreativeEvaluationReport(BaseModel):
    """Complete evaluation report for all creatives from a single agent run."""

    brand: str = Field(description="The brand being evaluated.")
    target_product: str = Field(description="The product being advertised.")
    target_search_trend: str = Field(description="The trend used for this creative set.")
    ad_copy_evaluations: list[AdCopyEvaluation] = Field(
        description="Evaluation results for each finalized ad copy."
    )
    visual_concept_evaluations: list[VisualConceptEvaluation] = Field(
        description="Evaluation results for each finalized visual concept."
    )
    summary: EvaluationSummary = Field(
        description="Aggregate statistics across all creatives."
    )
