"""Harvest creative_eval quality from a run's final session state — for free.

Every creative_agent run already executes ``creative_eval_agent``, whose tool
writes the full report (``CreativeEvaluationReport.model_dump()``) to
``state["creative_evaluation_report"]`` (see ``creative_eval/agent.py:112``). So
the DoE's H3 non-inferiority guardrail (pass-rate + mean score, judge fixed on
gemini-3.1-pro-preview @ global across all arms) costs zero extra quota: just
read the state each poll already returns.

Report shape (relevant slice):
    {"summary": {"overall_pass_rate": float, "avg_ad_copy_score": float,
                 "avg_visual_score": float},
     "ad_copy_evaluations":     [{"score": {"overall_score": float, "verdicts":
                 [{"dimension": str, "score": int(1-10)}, …]}}, …],
     "visual_concept_evaluations": [ …same score shape… ],
     "warnings": [str, …]}

Pure + offline; unit-tested in ``tests/test_quota_spread_analyze.py``.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

REPORT_KEY = "creative_evaluation_report"


def _all_scores(report: dict) -> list[dict]:
    """Flatten the per-creative ``score`` dicts across ad-copy + visual evals."""
    scores: list[dict] = []
    for key in ("ad_copy_evaluations", "visual_concept_evaluations"):
        for item in report.get(key) or []:
            score = item.get("score")
            if isinstance(score, dict):
                scores.append(score)
    return scores


def extract_quality(state: dict) -> dict | None:
    """Pull quality metrics from a run's final state.

    Returns ``None`` when the report is absent (a run that never reached eval).
    A present-but-empty report degrades to ``None`` metrics rather than crashing.

    Keys: ``pass_rate`` (summary.overall_pass_rate, 0–1), ``mean_score`` (grand
    mean of per-creative ``overall_score``), ``n_creatives``, ``dims`` (per-
    dimension mean of raw 1–10 verdict scores), ``warnings``.
    """
    report = (state or {}).get(REPORT_KEY)
    if not isinstance(report, dict):
        return None

    summary = report.get("summary") or {}
    scores = _all_scores(report)

    overalls = [
        float(s["overall_score"])
        for s in scores
        if s.get("overall_score") is not None
    ]
    mean_score = statistics.mean(overalls) if overalls else None

    # Prefer the report's own pass-rate; fall back to per-creative `passed` flags.
    pass_rate = summary.get("overall_pass_rate")
    if pass_rate is None and scores:
        passed = [1 for s in scores if s.get("passed")]
        pass_rate = len(passed) / len(scores)

    dim_scores: dict[str, list[float]] = defaultdict(list)
    for s in scores:
        for v in s.get("verdicts") or []:
            dim, raw = v.get("dimension"), v.get("score")
            if dim is not None and raw is not None:
                dim_scores[dim].append(float(raw))
    dims = {dim: statistics.mean(vals) for dim, vals in dim_scores.items() if vals}

    return {
        "pass_rate": pass_rate,
        "mean_score": mean_score,
        "n_creatives": len(scores),
        "dims": dims,
        "warnings": list(report.get("warnings") or []),
    }
