"""Render the quota-spread DoE figures via matplotlib (headless Agg, no browser).

Three money-shots, all from the committed run records (no live runs / no quota):

  1. ``research_slope.png`` — median research-phase wall-clock vs concurrency N,
     one line per arm. The primary H1 view: a flatter line = the quota spread
     absorbed the contention.
  2. ``totals_by_cell.png`` — total wall-clock per (arm, N), grouped bars.
  3. ``quality_by_arm.png`` — creative_eval mean score + pass-rate per arm
     (the H3 non-inferiority guardrail), harvested free from each run's state.

Deliberately NOT PaperBanana — its image path burns the same 2 RPM
``gemini-3.1-flash-image`` quota the experiment runs need.

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.quota_spread.doe_plot
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: no display/Chrome needed
import matplotlib.pyplot as plt  # noqa: E402  (backend must be set first)

from .analyze import (  # noqa: E402
    _no_quality,
    cell_summary,
    load_records,
    median_research_by_cell,
    quality_by_cell,
    research_slope_by_arm,
)
from .run_batch import RESULTS_ROOT  # noqa: E402

FIGURES_DIR = Path(__file__).parent / "figures"

# Stable per-arm colors; unknown arms fall back to the matplotlib cycle.
_ARM_COLORS = {
    "regional_25": "#2ca02c",  # the shipped #101 spread (treatment B)
    "global_3x": "#888888",    # baseline double-up (Arm A)
    "global_altbucket": "#1f77b4",  # distinct global bucket (Arm C)
}
_ARM_LABELS = {
    "regional_25": "regional_25\n(2.5 @ us-central1)",
    "global_3x": "global_3x\n(baseline double-up)",
    "global_altbucket": "global_altbucket\n(distinct global)",
}


def _color(arm: str) -> str:
    return _ARM_COLORS.get(arm, "#d62728")


def _plot_research_slope(records: list[dict], out: Path) -> Path:
    med = median_research_by_cell(records)
    slopes = research_slope_by_arm(records)
    by_arm: dict[str, dict[int, float]] = defaultdict(dict)
    for (arm, n), value in med.items():
        by_arm[arm][n] = value

    fig, ax = plt.subplots(figsize=(8, 5))
    for arm in sorted(by_arm):
        points = sorted(by_arm[arm].items())
        xs = [n for n, _ in points]
        ys = [y for _, y in points]
        ax.plot(
            xs,
            ys,
            marker="o",
            color=_color(arm),
            label=f"{arm} (slope={slopes.get(arm, 0.0):.1f}s/N)",
        )
    ax.set_xlabel("concurrency N (simultaneous runs)")
    ax.set_ylabel("median research-phase wall-clock (s)")
    ax.set_title("Research-phase contention: latency inflation vs concurrency (H1)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


def _plot_totals(records: list[dict], out: Path) -> Path:
    cells = cell_summary(records)
    arms = sorted({c["arm"] for c in cells.values()})
    loads = sorted({c["N"] for c in cells.values()})

    fig, ax = plt.subplots(figsize=(8, 5))
    width = 0.8 / max(1, len(arms))
    for i, arm in enumerate(arms):
        ys = []
        for n in loads:
            cell = cells.get(f"{arm}|{n}")
            ys.append(cell["total_s"]["median"] if cell and cell["total_s"] else 0.0)
        xs = [j + i * width for j in range(len(loads))]
        ax.bar(xs, ys, width=width, color=_color(arm), label=arm)
    ax.set_xticks([j + width * (len(arms) - 1) / 2 for j in range(len(loads))])
    ax.set_xticklabels([f"N={n}" for n in loads])
    ax.set_ylabel("median total wall-clock (s)")
    ax.set_title("Total run wall-clock per (arm, concurrency)")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


def _plot_quality(records: list[dict], out: Path, quality_fn) -> Path:
    quality = quality_by_cell(records, quality_fn)
    # Aggregate cells up to per-arm means for the non-inferiority guardrail view.
    by_arm_scores: dict[str, list[float]] = defaultdict(list)
    by_arm_pass: dict[str, list[float]] = defaultdict(list)
    for cell in quality.values():
        if cell["mean_score"] is not None:
            by_arm_scores[cell["arm"]].append(cell["mean_score"])
        if cell["pass_rate"] is not None:
            by_arm_pass[cell["arm"]].append(cell["pass_rate"])

    arms = sorted({c["arm"] for c in quality.values()})
    mean_scores = [
        (sum(by_arm_scores[a]) / len(by_arm_scores[a])) if by_arm_scores.get(a) else 0.0
        for a in arms
    ]
    pass_rates = [
        (sum(by_arm_pass[a]) / len(by_arm_pass[a])) if by_arm_pass.get(a) else 0.0
        for a in arms
    ]

    fig, ax = plt.subplots(figsize=(8, 5))
    xs = range(len(arms))
    width = 0.35
    ax.bar([x - width / 2 for x in xs], mean_scores, width, label="mean eval score", color="#1f77b4")
    ax.bar([x + width / 2 for x in xs], pass_rates, width, label="pass rate", color="#ff7f0e")
    ax.axhline(0.7, color="red", linestyle="--", alpha=0.6, label="pass threshold 0.7")
    ax.set_xticks(list(xs))
    ax.set_xticklabels(arms, rotation=0)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("normalized score / rate")
    ax.set_title("creative_eval quality by arm (H3 non-inferiority guardrail)")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


def render_all(records: list[dict], figures_dir: Path | str = FIGURES_DIR, quality_fn=None) -> list[Path]:
    """Render the three DoE figures; return the written PNG paths.

    ``quality_fn`` (state -> (pass, mean_score)) defaults to no-quality so the
    figure still renders (empty bars) before the Task 8 harvester is wired.
    """
    out_dir = Path(figures_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    qf = quality_fn or _no_quality
    return [
        _plot_research_slope(records, out_dir / "research_slope.png"),
        _plot_totals(records, out_dir / "totals_by_cell.png"),
        _plot_quality(records, out_dir / "quality_by_arm.png", qf),
    ]


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results-root", default=str(RESULTS_ROOT))
    p.add_argument("--figures-dir", default=str(FIGURES_DIR))
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    try:
        from .quality import extract_quality  # noqa: PLC0415

        def quality_fn(state: dict):
            q = extract_quality(state)
            return (q.get("pass_rate"), q.get("mean_score")) if q else (None, None)
    except Exception:  # noqa: BLE001
        quality_fn = _no_quality

    records = load_records(args.results_root)
    paths = render_all(records, args.figures_dir, quality_fn)
    for p in paths:
        print(f"wrote {p}", flush=True)


if __name__ == "__main__":
    main()
