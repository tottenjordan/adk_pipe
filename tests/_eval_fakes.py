"""Shared judge-response builders for the creative_eval tests.

The code keeps only the verdicts for the configured dimensions
(``normalize_verdicts``), so a fake judge must score every expected
dimension for its creative to be able to pass.
"""

from creative_eval.config import EvalConfig

AD_COPY_DIMENSIONS: tuple[str, ...] = tuple(EvalConfig().ad_copy_dimensions)
VISUAL_DIMENSIONS: tuple[str, ...] = tuple(EvalConfig().visual_dimensions)


def verdict_dicts(kind: str, score: int = 9) -> list[dict]:
    """One verdict dict per expected dimension of ``kind`` ("ad" or "visual")."""
    dims = AD_COPY_DIMENSIONS if kind == "ad" else VISUAL_DIMENSIONS
    return [
        {
            "dimension": d,
            "score": score,
            "verdict": "pass" if score >= 7 else "fail",
            "rationale": "r",
        }
        for d in dims
    ]


def all_verdict_dicts(score: int = 9) -> list[dict]:
    """Verdicts for both kinds' dimensions (the other kind's are dropped)."""
    return verdict_dicts("ad", score) + verdict_dicts("visual", score)
