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
    rationale: str = Field(description="1-2 sentence explanation of the score.")


class GateResult(BaseModel):
    """One binary compliance check (a "gate") against a concrete rule.

    LLM judges agree well with expert humans on binary compliance but only
    weakly on ranking creative quality, so gates — not the 1-10 scores — are
    the trustworthy part of the verdict. ``advisory`` gates are recorded but
    never fail a creative (set by code, not the judge).
    """

    gate: str = Field(
        description="The gate name (e.g. 'product_named', 'product_visible')."
    )
    passed: bool = Field(description="True when the creative meets the rule.")
    note: str = Field(
        default="",
        description="One short sentence of evidence, or why the gate does not apply.",
    )
    advisory: bool = Field(
        default=False,
        description="Recorded only; never part of gates_passed (set by code).",
    )


class GateResultIn(BaseModel):
    """A gate as the judge reports it (no code-set ``advisory`` field)."""

    gate: str = Field(
        description="The gate name, exactly as listed in the prompt (e.g. 'product_named')."
    )
    passed: bool = Field(description="True when the creative meets the rule.")
    note: str = Field(
        description="One short sentence of evidence, or why the gate does not apply."
    )


class JudgeScore(BaseModel):
    """The judge's raw scoring — only what the judge itself decides.

    ``gates`` is required and non-empty, so the judge cannot skip the checks;
    overall_score / passed / gates_passed / advisory are computed by code and
    are deliberately absent (the judge never grades itself).
    """

    verdicts: list[EvalVerdict] = Field(description="Per-dimension verdicts.")
    strengths: list[str] = Field(description="Top 2-3 strengths identified.")
    improvements: list[str] = Field(description="Top 2-3 suggested improvements.")
    gates: list[GateResultIn] = Field(
        min_length=1,
        description="One result per binary compliance gate listed in the prompt (all of them).",
    )


class AdCopyJudgeOutput(BaseModel):
    """The ad-copy judge's response schema (mapped to AdCopyEvaluation in code)."""

    original_id: int = Field(description="Maps to FinalAdCopy.original_id.")
    headline: str = Field(description="The headline that was evaluated.")
    tone_style: str = Field(description="The tone/style of this ad copy.")
    score: JudgeScore = Field(description="Scoring details.")


class VisualJudgeOutput(BaseModel):
    """The visual judge's response schema (mapped to VisualConceptEvaluation in code)."""

    ad_copy_id: int = Field(description="Maps to VisualConceptFinal.ad_copy_id.")
    concept_name: str = Field(description="The concept that was evaluated.")
    score: JudgeScore = Field(description="Scoring details.")


class CreativeScore(BaseModel):
    """Aggregate score for a single creative (ad copy or visual concept).

    ``passed`` = ``overall_score >= passing_threshold`` AND ``gates_passed``.
    """

    overall_score: float = Field(
        description="Unweighted mean of the per-dimension scores, normalized to 0.0-1.0.",
        ge=0.0,
        le=1.0,
    )
    passed: bool = Field(
        description="True if overall_score >= passing threshold and every non-advisory gate passed."
    )
    verdicts: list[EvalVerdict] = Field(description="Per-dimension verdicts.")
    strengths: list[str] = Field(description="Top 2-3 strengths identified.")
    improvements: list[str] = Field(description="Top 2-3 suggested improvements.")
    # Defaults keep reports written before the gates existed valid.
    gates: list[GateResult] = Field(
        default_factory=list,
        description="One result per binary compliance gate listed in the prompt.",
    )
    gates_passed: bool = Field(
        default=True,
        description="True when every non-advisory gate passed (recomputed by code).",
    )


class AdCopyEvaluation(BaseModel):
    """Evaluation result for a single finalized ad copy."""

    original_id: int = Field(description="Maps to FinalAdCopy.original_id.")
    headline: str = Field(description="The headline that was evaluated.")
    tone_style: str = Field(description="The tone/style of this ad copy.")
    score: CreativeScore = Field(description="Scoring details.")
    # Copied from the creative by code (not the judge) so brand history can tell
    # which angles scored well; "" for old reports and copies without one.
    angle_id: str = Field(
        default="", description="Creative brief angle id (set by code)."
    )


class VisualConceptEvaluation(BaseModel):
    """Evaluation result for a single finalized visual concept."""

    ad_copy_id: int = Field(description="Maps to VisualConceptFinal.ad_copy_id.")
    concept_name: str = Field(description="The concept that was evaluated.")
    score: CreativeScore = Field(description="Scoring details.")
    image_judged: bool = Field(
        default=False,
        description="True when the rendered image was judged; False = the prompt text only (set by code).",
    )
    # Copied from the concept by code (not the judge) for brand history; ""
    # for old reports.
    visual_style: str = Field(
        default="", description="Style family used (set by code)."
    )
    angle_id: str = Field(
        default="", description="Creative brief angle id (set by code)."
    )


class EvaluationSummary(BaseModel):
    """Aggregate statistics for the evaluation report."""

    total_ad_copies: int = Field(description="Number of ad copies evaluated.")
    ad_copies_passed: int = Field(description="Number of ad copies that passed.")
    avg_ad_copy_score: float = Field(description="Average score across ad copies.")
    total_visual_concepts: int = Field(
        description="Number of visual concepts evaluated."
    )
    visual_concepts_passed: int = Field(
        description="Number of visual concepts that passed."
    )
    avg_visual_score: float = Field(description="Average score across visual concepts.")
    overall_pass_rate: float = Field(
        description="Combined pass rate across all creatives (0.0-1.0)."
    )
    weakest_dimensions: list[str] = Field(
        description="Dimensions with the lowest average scores across all creatives."
    )
    gates_pass_rate: float | None = Field(
        default=None,
        description=(
            "Share of judged creatives whose non-advisory gates all passed (0.0-1.0). "
            "Creatives whose judge call failed (improvements == ['evaluation_failed']) "
            "are excluded from numerator and denominator; None when no creative was "
            "judged (or for pre-gate reports). Without a brief the brief-dependent "
            "gates auto-pass (note 'no brief'), so no-brief runs read higher."
        ),
    )


class CreativeEvaluationReport(BaseModel):
    """Complete evaluation report for all creatives from a single agent run."""

    brand: str = Field(description="The brand being evaluated.")
    target_product: str = Field(description="The product being advertised.")
    target_search_trend: str = Field(
        description="The trend used for this creative set."
    )
    ad_copy_evaluations: list[AdCopyEvaluation] = Field(
        description="Evaluation results for each finalized ad copy."
    )
    visual_concept_evaluations: list[VisualConceptEvaluation] = Field(
        description="Evaluation results for each finalized visual concept."
    )
    summary: EvaluationSummary = Field(
        description="Aggregate statistics across all creatives."
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Human-readable notes about degraded/incomplete pipeline steps (e.g. research retries exhausted).",
    )
    # The judge has no model fallback (a silent swap would skew pass rates), so
    # record which model graded the report. Default "" keeps old reports valid.
    judge_model: str = Field(default="", description="Model that judged this report.")
    # 0.7 = the threshold every pre-gate report was graded against.
    passing_threshold: float = Field(
        default=0.7, description="Minimum overall_score for a creative to pass."
    )
    brief_used: bool = Field(
        default=False,
        description="True when the judge saw the structured creative brief.",
    )
    # Opt-in rating-driven learning (creative_agent rating_signals): tags runs
    # whose generation was steered by human ratings, so judge calibration can
    # separate them. Defaults keep old reports valid.
    learning_used: bool = Field(
        default=False,
        description="True when rating-driven learning was applied to this run.",
    )
    learning_flags: list[str] = Field(
        default_factory=list,
        description="The learning effects that changed the run: 'guidance', 'styles' and each rating strictness flag (e.g. 'product_not_visible').",
    )
