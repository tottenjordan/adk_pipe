"""Judge-human agreement over ``creative_ratings`` rows (pure, no numpy).

For each slice (all ratings, then per ``kind``):

- ``judge_passed`` / ``judge_gates_passed``: raw agreement and Cohen's kappa
  between the judge's boolean and the human verdict (``pass`` = True), over the
  rows where the judge value is known. Kappa is reported as n/a (``None`` with a
  ``reason``) when there are no pairs or when either rater used a single class:
  kappa is then undefined (both) or pinned at 0 / below (one), whatever the
  agreement, so a number would mislead.
- ``score_spearman``: Spearman's rho between ``judge_overall`` (0-1) and the
  human 1-5 ``score`` once at least ``MIN_SPEARMAN_PAIRS`` rows have both.

Versioned: agreement is only meaningful within one judge version (gates, wording,
scoring rules and model; docs/notes/judge-calibration.md), so the report keeps the
ratings whose snapshotted ``judge_version`` equals creative_eval's current
``JUDGE_VERSION`` by default (``""``/NULL = a pre-versioning report, i.e. an earlier
judge) and counts the paired ratings it left out (``excluded_other_versions``).
Within that version, ``by_learning`` splits the same blocks by whether opt-in rating
learning steered the run (``learning_used``).

Shared by ``GET /ratings/{user}/calibration`` and ``scripts/eval_calibration.py``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# creative_eval is a dependency-light sibling package shipped in the api image
# (the Dockerfile copies every flat package; PYTHONPATH=/app): importing the one
# constant keeps a single source of truth (no mirrored copy + drift test).
from creative_eval.dimensions import JUDGE_VERSION

KINDS = ("visual", "ad_copy")
MIN_SPEARMAN_PAIRS = 5
#: Paired ratings before the UI reports the agreement (docs/notes/judge-calibration.md).
READY_MIN_RATINGS = 20


def _bool(value: Any) -> bool | None:
    """A judge boolean from BigQuery (bool) or a CSV export ("true"/"false"/"")."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "t", "yes"):
            return True
        if v in ("false", "0", "f", "no"):
            return False
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def cohen_kappa(pairs: Sequence[tuple[bool, bool]]) -> dict[str, Any]:
    """``{n, agreement, kappa, reason}`` for (judge, human) boolean pairs."""
    n = len(pairs)
    if n == 0:
        return {"n": 0, "agreement": None, "kappa": None, "reason": "no_pairs"}
    agree = sum(1 for j, h in pairs if j == h)
    po = agree / n
    pj = sum(1 for j, _ in pairs if j) / n
    ph = sum(1 for _, h in pairs if h) / n
    judge_single = pj in (0.0, 1.0)
    human_single = ph in (0.0, 1.0)
    reason = None
    if judge_single and human_single:
        reason = "single_class"
    elif judge_single:
        reason = "judge_single_class"
    elif human_single:
        reason = "human_single_class"
    if reason:
        return {"n": n, "agreement": po, "kappa": None, "reason": reason}
    pe = pj * ph + (1 - pj) * (1 - ph)
    return {"n": n, "agreement": po, "kappa": (po - pe) / (1 - pe), "reason": None}


def _ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks, ties get their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(pairs: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """``{n, rho, reason}``: Pearson correlation of the tie-averaged ranks."""
    n = len(pairs)
    if n < MIN_SPEARMAN_PAIRS:
        return {"n": n, "rho": None, "reason": "too_few_pairs"}
    rx = _ranks([x for x, _ in pairs])
    ry = _ranks([y for _, y in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return {"n": n, "rho": None, "reason": "constant_values"}
    return {"n": n, "rho": cov / math.sqrt(vx * vy), "reason": None}


def _human(row: Mapping[str, Any]) -> bool | None:
    verdict = row.get("verdict")
    return (
        {"pass": True, "fail": False}.get(verdict) if isinstance(verdict, str) else None
    )


def calibration_block(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    passed: list[tuple[bool, bool]] = []
    gates: list[tuple[bool, bool]] = []
    scores: list[tuple[float, float]] = []
    for row in rows:
        human = _human(row)
        if human is None:
            continue
        if (j := _bool(row.get("judge_passed"))) is not None:
            passed.append((j, human))
        if (g := _bool(row.get("judge_gates_passed"))) is not None:
            gates.append((g, human))
        overall, score = _number(row.get("judge_overall")), _number(row.get("score"))
        if overall is not None and score is not None:
            scores.append((overall, score))
    return {
        "n": sum(1 for r in rows if _human(r) is not None),
        "judge_passed": cohen_kappa(passed),
        "judge_gates_passed": cohen_kappa(gates),
        "score_spearman": spearman(scores),
    }


def _version(row: Mapping[str, Any]) -> str:
    value = row.get("judge_version")
    return value.strip() if isinstance(value, str) else ""


def _paired(row: Mapping[str, Any]) -> bool:
    """A human verdict with a known judge pass verdict (the UI's readiness count)."""
    return _human(row) is not None and _bool(row.get("judge_passed")) is not None


def calibration_report(
    rows: Iterable[Mapping[str, Any]], judge_version: str | None = JUDGE_VERSION
) -> dict[str, Any]:
    """The full report over one judge version (``None`` = every version):
    overall + per-kind + per-learning blocks, rating/session counts, and the
    number of paired ratings from other (or unknown) versions left out."""
    rows = list(rows)
    if judge_version is not None:
        kept = [r for r in rows if _version(r) == judge_version]
        excluded = sum(1 for r in rows if _version(r) != judge_version and _paired(r))
    else:
        kept, excluded = rows, 0
    learned = [r for r in kept if _bool(r.get("learning_used")) is True]
    not_learned = [r for r in kept if _bool(r.get("learning_used")) is not True]
    return {
        "n": len(kept),
        "sessions": len({r.get("session_id") for r in kept if r.get("session_id")}),
        "ready_min_ratings": READY_MIN_RATINGS,
        "overall": calibration_block(kept),
        "by_kind": {
            kind: calibration_block([r for r in kept if r.get("kind") == kind])
            for kind in KINDS
        },
        "judge_version": judge_version,
        "excluded_other_versions": excluded,
        "by_learning": {
            "learned": calibration_block(learned),
            "not_learned": calibration_block(not_learned),
        },
    }


_REASONS = {
    "no_pairs": "no paired ratings",
    "single_class": "both judge and humans gave a single verdict",
    "judge_single_class": "the judge gave a single verdict",
    "human_single_class": "humans gave a single verdict",
    "too_few_pairs": f"fewer than {MIN_SPEARMAN_PAIRS} paired scores",
    "constant_values": "a constant score",
}


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _fmt_kappa(k: Mapping[str, Any]) -> str:
    if k["n"] == 0:
        return "n=0"
    kappa = (
        f"kappa {k['kappa']:.2f}"
        if k["kappa"] is not None
        else f"kappa n/a ({_REASONS.get(k['reason'], k['reason'])})"
    )
    return f"n={k['n']}, agreement {_pct(k['agreement'])}, {kappa}"


def _fmt_rho(s: Mapping[str, Any]) -> str:
    if s["rho"] is None:
        return f"n={s['n']}, rho n/a ({_REASONS.get(s['reason'], s['reason'])})"
    return f"n={s['n']}, rho {s['rho']:.2f}"


def format_report(report: Mapping[str, Any]) -> str:
    """Plain-text rendering (the calibration script's output)."""
    lines = [
        f"Ratings: {report['n']} across {report['sessions']} runs "
        f"(report agreement from {report['ready_min_ratings']} paired ratings)",
        f"Judge version: {report.get('judge_version') or 'all'}",
    ]
    if excluded := report.get("excluded_other_versions"):
        lines.append(
            f"  ({excluded} paired ratings from other judge versions not counted)"
        )
    blocks = [("all", report["overall"])] + [(k, report["by_kind"][k]) for k in KINDS]
    by_learning = report.get("by_learning") or {}
    blocks += [
        (name, by_learning[key])
        for name, key in (("learned", "learned"), ("not learned", "not_learned"))
        if key in by_learning
    ]
    for name, block in blocks:
        lines += [
            f"[{name}] {block['n']} ratings",
            f"  judge pass vs human:  {_fmt_kappa(block['judge_passed'])}",
            f"  judge gates vs human: {_fmt_kappa(block['judge_gates_passed'])}",
            f"  overall vs score:     {_fmt_rho(block['score_spearman'])}",
        ]
    return "\n".join(lines)
