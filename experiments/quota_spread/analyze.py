"""Reduce quota-spread run records into a tidy CSV + the H1 contention slope.

Everything here is pure and offline: it loads the JSON records written by
``run_batch``/``run_doe`` and emits (1) one tidy CSV row per run and (2) a
per-arm summary whose headline number is the **slope of median research-phase
wall-clock vs concurrency N** — flatter slope ⇒ the quota spread absorbed the
contention (H1). Quality columns are filled by an injected ``quality_fn`` (Task 8;
defaults to blanks) so this module has no dependency on the eval-state shape.

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.quota_spread.analyze \\
        [--results-root experiments/quota_spread/results] [--csv runs.csv]
"""

from __future__ import annotations

import argparse
import csv as _csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Callable

from .run_batch import RESULTS_ROOT

# (state) -> (pass: bool|None, mean_score: float|None); default = no quality.
QualityFn = Callable[[dict], tuple[bool | None, float | None]]


def _no_quality(_state: dict) -> tuple[None, None]:
    return None, None


# Derived artifacts this module (and doe_plot) write into the results root — not
# run records, so load_records must never load them back as runs.
_NON_RECORD_JSON = {"manifest.json", "summary.json"}


def load_records(results_root: Path | str = RESULTS_ROOT) -> list[dict]:
    """Load every per-run JSON under ``results_root`` (skips derived artifacts)."""
    root = Path(results_root)
    records: list[dict] = []
    for path in sorted(root.rglob("*.json")):
        if path.name in _NON_RECORD_JSON:
            continue
        records.append(json.loads(path.read_text()))
    return records


def _done(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("status") == "done"]


def median_research_by_cell(records: list[dict]) -> dict[tuple[str, int], float]:
    """Median research_s per (arm, N), over ``done`` runs with a research_s."""
    buckets: dict[tuple[str, int], list[float]] = defaultdict(list)
    for r in _done(records):
        val = r.get("research_s")
        if val is not None:
            buckets[(r["arm"], r["concurrency"])].append(float(val))
    return {cell: statistics.median(vals) for cell, vals in buckets.items() if vals}


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (pct in [0,1]); matches numpy's default."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def research_tail_by_cell(
    records: list[dict], pct: float = 0.9
) -> dict[tuple[str, int], dict]:
    """Per-(arm, N) research-phase tail: {p90, max, n} over ``done`` runs.

    The honest contention view: 429s are absorbed by genai HTTP retry *below*
    the ADK model-call boundary, so the *median* barely moves with concurrency.
    The inflation shows up in the TAIL — a run that loses the retry lottery
    repeatedly. p90/max expose it where the median hides it.
    """
    buckets: dict[tuple[str, int], list[float]] = defaultdict(list)
    for r in _done(records):
        val = r.get("research_s")
        if val is not None:
            buckets[(r["arm"], r["concurrency"])].append(float(val))
    return {
        cell: {"p90": _percentile(vals, pct), "max": max(vals), "n": len(vals)}
        for cell, vals in buckets.items()
        if vals
    }


def error_rate_by_cell(records: list[dict]) -> dict[tuple[str, int], float]:
    """Fraction of runs with status=='error' per (arm, N) — the reliability view."""
    total: dict[tuple[str, int], int] = defaultdict(int)
    errors: dict[tuple[str, int], int] = defaultdict(int)
    for r in records:
        cell = (r["arm"], r["concurrency"])
        total[cell] += 1
        if r.get("status") == "error":
            errors[cell] += 1
    return {cell: errors[cell] / n for cell, n in total.items() if n}


def research_slope(cell_medians: dict[int, float]) -> float:
    """Least-squares slope of median research_s vs N. <2 points ⇒ 0.0.

    ``cell_medians`` maps concurrency N -> median research_s for ONE arm.
    """
    points = sorted(cell_medians.items())
    if len(points) < 2:
        return 0.0
    xs = [float(n) for n, _ in points]
    ys = [float(y) for _, y in points]
    return statistics.linear_regression(xs, ys).slope


def research_slope_by_arm(records: list[dict]) -> dict[str, float]:
    """Per-arm research-vs-N slope (the H1 headline: baseline steep, spread flat)."""
    by_arm: dict[str, dict[int, float]] = defaultdict(dict)
    for (arm, n), med in median_research_by_cell(records).items():
        by_arm[arm][n] = med
    return {arm: research_slope(cells) for arm, cells in by_arm.items()}


def to_tidy_rows(records: list[dict], quality_fn: QualityFn = _no_quality) -> list[dict]:
    """One tidy dict per run (the CSV schema). ``quality_fn`` reads final state."""
    rows: list[dict] = []
    for r in records:
        eval_pass, eval_mean = quality_fn(r.get("state") or {})
        rows.append(
            {
                "arm": r.get("arm"),
                "N": r.get("concurrency"),
                "batch": r.get("batch_id"),
                "session": r.get("session_id"),
                "status": r.get("status"),
                "research_s": r.get("research_s"),
                "total_s": r.get("total_s"),
                "eval_pass": eval_pass,
                "eval_mean": eval_mean,
                "count_429": r.get("count_429"),
                "exhaustion": ";".join(r.get("exhaustion") or []),
            }
        )
    return rows


_CSV_FIELDS = [
    "arm",
    "N",
    "batch",
    "session",
    "status",
    "research_s",
    "total_s",
    "eval_pass",
    "eval_mean",
    "count_429",
    "exhaustion",
]


def write_csv(rows: list[dict], path: Path | str) -> Path:
    """Write tidy rows to ``path`` (one run per row). Returns the path."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in _CSV_FIELDS})
    return out


def _stats(values: list[float]) -> dict | None:
    if not values:
        return None
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def cell_summary(records: list[dict]) -> dict[str, dict]:
    """Per-(arm,N) median/min/max for research_s and total_s (keyed 'arm|N')."""
    buckets: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in _done(records):
        buckets[(r["arm"], r["concurrency"])].append(r)
    out: dict[str, dict] = {}
    for (arm, n), rs in sorted(buckets.items()):
        research = [float(r["research_s"]) for r in rs if r.get("research_s") is not None]
        total = [float(r["total_s"]) for r in rs if r.get("total_s") is not None]
        out[f"{arm}|{n}"] = {
            "arm": arm,
            "N": n,
            "n_done": len(rs),
            "research_s": _stats(research),
            "total_s": _stats(total),
        }
    return out


def build_analysis(records: list[dict], quality_fn: QualityFn = _no_quality) -> dict:
    """Full analysis blob: per-cell stats, per-arm research slopes, run counts."""
    return {
        "n_runs": len(records),
        "n_done": len(_done(records)),
        "cells": cell_summary(records),
        "research_slope_by_arm": research_slope_by_arm(records),
        "quality_by_cell": quality_by_cell(records, quality_fn),
    }


def quality_by_cell(
    records: list[dict], quality_fn: QualityFn = _no_quality
) -> dict[str, dict]:
    """Per-(arm,N) mean pass-rate + mean eval score from each run's final state.

    ``quality_fn`` returns per-run FLOAT metrics (a run's own overall_pass_rate
    and grand-mean score), so both are aggregated across a cell by MEAN — not by
    bool-counting, which would collapse a 0.667 rate to True and lose the signal.
    """
    buckets: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in _done(records):
        buckets[(r["arm"], r["concurrency"])].append(r)
    out: dict[str, dict] = {}
    for (arm, n), rs in sorted(buckets.items()):
        rates: list[float] = []
        means: list[float] = []
        for r in rs:
            rate, mean = quality_fn(r.get("state") or {})
            if rate is not None:
                rates.append(float(rate))
            if mean is not None:
                means.append(float(mean))
        out[f"{arm}|{n}"] = {
            "arm": arm,
            "N": n,
            "pass_rate": statistics.mean(rates) if rates else None,
            "mean_score": statistics.mean(means) if means else None,
            "n_scored": len(means),
        }
    return out


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results-root", default=str(RESULTS_ROOT))
    p.add_argument("--csv", default=str(RESULTS_ROOT / "runs.csv"))
    p.add_argument("--summary", default=str(RESULTS_ROOT / "analysis.json"))
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    # Quality harvest is wired in Task 8; import lazily so analyze stands alone.
    try:
        from .quality import extract_quality  # noqa: PLC0415

        def quality_fn(state: dict) -> tuple[bool | None, float | None]:
            q = extract_quality(state)
            return (q.get("pass_rate"), q.get("mean_score")) if q else (None, None)
    except Exception:  # noqa: BLE001 — quality is optional enrichment
        quality_fn = _no_quality

    records = load_records(args.results_root)
    rows = to_tidy_rows(records, quality_fn)
    csv_path = write_csv(rows, args.csv)
    analysis = build_analysis(records, quality_fn)
    Path(args.summary).write_text(json.dumps(analysis, indent=2, default=str))
    print(f"wrote {csv_path} ({len(rows)} rows) and {args.summary}", flush=True)
    print("research_slope_by_arm:", analysis["research_slope_by_arm"], flush=True)


if __name__ == "__main__":
    main()
