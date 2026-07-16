"""Render the creative_latency results into static PNG figures via matplotlib.

Companion to :mod:`plot.py`. ``plot.py`` builds the interactive ``report.html``
and *can* emit PNGs, but its PNG path goes through kaleido, which needs a working
headless Chrome — unavailable in some sandboxes, so PNG export silently degrades
to "html only" there. This module renders the same comparisons with matplotlib
(pure Agg backend, no browser), so static figures regenerate anywhere.

Reads every ``results/<config>/_summary.json`` (reusing :mod:`plot.py`'s loaders)
and writes:

  1. ``latency_phases.png``  — stacked per-phase wall-clock per config, with the
     total + %-delta-vs-baseline annotated on each bar.
  2. ``latency_totals.png``  — total wall-clock per config alongside the 429/503
     rate-limit counts (median + max) that expose the quota ceiling.

Both read purely from the committed summary JSONs, so no live runs (and no model
quota) are consumed. Deliberately NOT PaperBanana: its image path burns the same
2 RPM ``gemini-3.1-flash-image`` quota the runs need.

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.creative_latency.render_static
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: no display/Chrome needed
import matplotlib.pyplot as plt  # noqa: E402  (backend must be set first)

from experiments.creative_latency.plot import (  # noqa: E402
    FIGURES_DIR,
    PHASE_COLORS,
    RESULTS_ROOT,
    _median,
    _phases_present,
    load_config_summaries,
)

# Prefer baseline first, then the levers; any unknown config keeps insertion order.
_CONFIG_ORDER = ["baseline", "fewer_pro", "flash_critics", "parallel_img"]
_CONFIG_LABELS = {
    "baseline": "baseline",
    "fewer_pro": "Lever A\n(fewer pro)",
    "flash_critics": "Lever C\n(flash critics)",
    "parallel_img": "Lever B\n(parallel img)",
}
_TOTAL_COLORS = {
    "baseline": "#888888",
    "fewer_pro": "#2ca02c",
    "flash_critics": "#1f77b4",
    "parallel_img": "#d62728",
}


def _ordered_configs(summaries: dict[str, dict]) -> list[str]:
    known = [c for c in _CONFIG_ORDER if c in summaries]
    return known + [c for c in summaries if c not in known]


def _label(config: str) -> str:
    return _CONFIG_LABELS.get(config, config)


def _total(summaries: dict[str, dict], config: str) -> float:
    return _median(summaries[config].get("total_wall_s"))


def render_stacked_phases(summaries: dict[str, dict], out_path: Path) -> Path:
    """Stacked per-phase bar per config; total + %-vs-baseline annotated."""
    configs = _ordered_configs(summaries)
    phases = _phases_present(summaries)
    base_total = _total(summaries, "baseline") if "baseline" in summaries else None

    fig, ax = plt.subplots(figsize=(9, 6))
    x = range(len(configs))
    bottoms = [0.0] * len(configs)
    for phase in phases:
        vals = [
            _median(summaries[c].get("phase_wall_s", {}).get(phase)) for c in configs
        ]
        ax.bar(
            x,
            vals,
            bottom=bottoms,
            label=phase,
            color=PHASE_COLORS.get(phase),
            edgecolor="white",
            linewidth=0.5,
        )
        bottoms = [b + v for b, v in zip(bottoms, vals)]

    for i, config in enumerate(configs):
        total = _total(summaries, config)
        text = f"{total:.0f}s"
        if base_total and config != "baseline":
            text += f"\n({(total - base_total) / base_total * 100:+.1f}%)"
        ax.text(
            i,
            bottoms[i] + 6,
            text,
            ha="center",
            va="bottom",
            fontweight="bold",
            fontsize=10,
        )

    ax.set_xticks(list(x))
    ax.set_xticklabels([_label(c) for c in configs])
    ax.set_ylabel("median wall-clock (seconds)")
    ax.set_title(
        "creative_agent latency by config — stacked phase breakdown\n"
        "(3 live trials each; median)"
    )
    ax.legend(title="phase", bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=9)
    ax.set_ylim(0, (max(bottoms) if bottoms else 1) * 1.15)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return out_path


def render_totals_and_ratelimits(summaries: dict[str, dict], out_path: Path) -> Path:
    """Total wall-clock per config + 429/503 counts (the quota-ceiling signal)."""
    configs = _ordered_configs(summaries)
    x = range(len(configs))
    base_total = _total(summaries, "baseline") if "baseline" in summaries else None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    totals = [_total(summaries, c) for c in configs]
    ax1.bar(
        x,
        totals,
        color=[_TOTAL_COLORS.get(c, "#5B8FF9") for c in configs],
        edgecolor="black",
        linewidth=0.6,
    )
    if base_total:
        ax1.axhline(base_total, ls="--", color="gray", alpha=0.7)
    for i, config in enumerate(configs):
        text = f"{totals[i]:.0f}s"
        if base_total and config != "baseline":
            text += f"\n{(totals[i] - base_total) / base_total * 100:+.1f}%"
        ax1.text(i, totals[i] + 4, text, ha="center", va="bottom", fontweight="bold")
    ax1.set_xticks(list(x))
    ax1.set_xticklabels([_label(c) for c in configs])
    ax1.set_ylabel("median total wall-clock (s)")
    ax1.set_title("Total wall-clock by config")
    ax1.set_ylim(0, (max(totals) if totals else 1) * 1.18)
    ax1.grid(axis="y", alpha=0.3)

    width = 0.38
    med = [
        float((summaries[c].get("http_429_503") or {}).get("median", 0))
        for c in configs
    ]
    mx = [
        float((summaries[c].get("http_429_503") or {}).get("max", 0)) for c in configs
    ]
    ax2.bar(
        [i - width / 2 for i in x],
        med,
        width,
        label="median",
        color="#ff7f0e",
        edgecolor="black",
        linewidth=0.5,
    )
    ax2.bar(
        [i + width / 2 for i in x],
        mx,
        width,
        label="max",
        color="#ffbb78",
        edgecolor="black",
        linewidth=0.5,
    )
    ax2.set_xticks(list(x))
    ax2.set_xticklabels([_label(c) for c in configs])
    ax2.set_ylabel("HTTP 429/503 count per run")
    ax2.set_title(
        "Rate-limit hits (quota pressure)\nLever B spikes → quota-bound proof"
    )
    ax2.legend()
    ax2.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return out_path


def render_all(results_root: Path, figures_dir: Path) -> list[Path]:
    """Render every static figure; returns the written paths (empty if no data)."""
    summaries = load_config_summaries(results_root)
    if not summaries:
        return []
    return [
        render_stacked_phases(summaries, figures_dir / "latency_phases.png"),
        render_totals_and_ratelimits(summaries, figures_dir / "latency_totals.png"),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-root",
        type=Path,
        default=RESULTS_ROOT,
        help="Directory holding <config>/_summary.json files.",
    )
    parser.add_argument(
        "--figures-dir",
        type=Path,
        default=FIGURES_DIR,
        help="Directory to write PNG figures into.",
    )
    args = parser.parse_args()
    written = render_all(args.results_root, args.figures_dir)
    if not written:
        print(f"No results found under {args.results_root} — nothing to render.")
        return
    for path in written:
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
