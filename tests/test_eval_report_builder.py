"""Both report entry points share one builder (``evaluate.build_report``).

``evaluate_creatives`` (standalone) and ``agent.evaluate_all_creatives`` (the
pipeline) must stamp the same summary, judge metadata and judge warnings; the
pipeline adds only the run's degradation notes (first) and the learning record.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent_common import collect_degradation_warnings
from creative_eval.agent import evaluate_all_creatives
from creative_eval.config import EvalConfig
from creative_eval.dimensions import JUDGE_VERSION
from creative_eval.evaluate import evaluate_creatives
from creative_eval.gates import NOT_REPORTED_NOTE
from creative_eval.schemas import (
    AdCopyEvaluation,
    CreativeScore,
    GateResult,
    VisualConceptEvaluation,
)

CAMPAIGN_STATE = {
    "brand": "ACME",
    "target_product": "Rocket Skates",
    "target_audience": "runners",
    "key_selling_points": "fast",
    "target_search_trends": "roadrunner",
}
CAMPAIGN = {
    **{k: v for k, v in CAMPAIGN_STATE.items() if k != "target_search_trends"},
    "target_search_trend": "roadrunner",
}
AD_COPY = {"original_id": 1, "headline": "Beep", "tone_style": "Playful"}
CONCEPT = {"ad_copy_id": 1, "concept_name": "Hero"}
APPLIED = {"applied": True, "signals": "prefer bold", "strictness": ["weak_cta"]}


def _score(passed: bool = True) -> CreativeScore:
    return CreativeScore(
        overall_score=0.9 if passed else 0.3,
        passed=passed,
        verdicts=[],
        strengths=[],
        improvements=["tighten the CTA"],
        gates=[
            GateResult(
                gate="brand_cue_present",
                passed=True,
                note=NOT_REPORTED_NOTE,
                advisory=True,
            )
        ],
        gates_passed=True,
    )


def _evals():
    return (
        [
            AdCopyEvaluation(
                original_id=1, headline="Beep", tone_style="Playful", score=_score()
            )
        ],
        [VisualConceptEvaluation(ad_copy_id=1, concept_name="Hero", score=_score())],
    )


def _standalone():
    with (
        patch("creative_eval.evaluate._get_client", return_value=MagicMock()),
        patch(
            "creative_eval.evaluate.evaluate_all_concurrently", return_value=_evals()
        ),
    ):
        return evaluate_creatives(CAMPAIGN, [AD_COPY], [CONCEPT], EvalConfig())


def _pipeline(extra: dict | None = None):
    state = {
        **CAMPAIGN_STATE,
        "ad_copy_critique": {"ad_copies": [AD_COPY]},
        "final_visual_concepts": {"visual_concepts": [CONCEPT]},
        **(extra or {}),
    }
    ctx = MagicMock(state=state)
    with patch("creative_eval.agent.evaluate_all_concurrently", return_value=_evals()):
        result = evaluate_all_creatives(ctx)
    return state, result


JUDGE_WARNING = "2 checks not reported by the judge (passed as not checked)"


def test_standalone_report_has_judge_warnings_and_no_learning():
    report = _standalone()
    assert report.warnings == [JUDGE_WARNING]
    assert report.learning_used is False and report.learning_flags == []
    assert report.judge_version == JUDGE_VERSION
    assert report.summary.total_ad_copies == 1


def test_pipeline_report_adds_run_notes_first_and_learning():
    extra = {
        "rating_signals_applied": APPLIED,
        "trend_situation_draft__retry_exhausted": True,
    }
    state, result = _pipeline(extra)
    report = state["creative_evaluation_report"]
    notes = collect_degradation_warnings(state)
    assert notes  # the marker produced a run note
    assert report["warnings"] == [*notes, JUDGE_WARNING]
    assert report["learning_used"] is True
    assert report["learning_flags"] == ["guidance", "weak_cta"]
    assert result["status"] == "success"


def test_both_entry_points_build_the_same_report():
    """Without run notes or learning the two reports are identical."""
    state, _ = _pipeline()
    piped = state["creative_evaluation_report"]
    assert _standalone().model_dump() == {**piped, "brief_used": False}


def test_failed_creatives_carry_the_judge_improvements():
    def failing():
        ad, vis = _evals()
        ad[0].score.passed = False
        return ad, vis

    state = {
        **CAMPAIGN_STATE,
        "ad_copy_critique": {"ad_copies": [AD_COPY]},
        "final_visual_concepts": {"visual_concepts": [CONCEPT]},
    }
    with patch("creative_eval.agent.evaluate_all_concurrently", return_value=failing()):
        result = evaluate_all_creatives(MagicMock(state=state))
    assert result["failed_creatives"] == [
        {
            "type": "ad_copy",
            "id": 1,
            "name": "Beep",
            "overall_score": 0.9,
            "improvements": ["tighten the CTA"],
            "failed_gates": [],
        }
    ]
