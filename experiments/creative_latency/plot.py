"""Render the creative_latency results into an interactive report + static PNGs.

Reads every ``results/<config>/_summary.json`` (and the per-trial JSON records
for distributions) and builds Plotly figures:

  1. stacked per-phase latency bar per config
  2. per-trial distribution (box + jittered scatter) for the baseline
  3. model-call counts by phase per config (grouped bar)
  4. baseline-vs-variant grouped bar of total + per-phase wall-clock
  5. 429/503 counts per config (only when any data is present)

Outputs a single self-contained ``report.html`` (interactive, needs no browser
to *view*) plus ``figures/<name>.png`` for embedding in the writeup. PNG export
uses kaleido, which needs a working headless Chrome; it is best-effort — a
missing/broken Chrome degrades to "html only" with a warning, never an error.

Deliberately NOT PaperBanana: its image path burns the same 2 RPM
``gemini-3.1-flash-image`` quota the runs need; Plotly renders purely from data.

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.creative_latency.plot
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import plotly.graph_objects as go

RESULTS_ROOT = Path(__file__).parent / "results"
REPORT_PATH = Path(__file__).parent / "report.html"
FIGURES_DIR = Path(__file__).parent / "figures"

# Stable phase order + palette so configs line up across figures.
PHASE_ORDER = [
    "orchestrator",
    "research",
    "ad_copy",
    "visual_concepts",
    "image_gen",
    "eval",
    "persistence",
    "runserver",
    "other",
]
PHASE_COLORS = {
    "orchestrator": "#5B8FF9",
    "research": "#61DDAA",
    "ad_copy": "#F6BD16",
    "visual_concepts": "#7262FD",
    "image_gen": "#FF9845",
    "eval": "#F6903D",
    "persistence": "#9FB40F",
    "runserver": "#C2C8D5",
    "other": "#D3D3D3",
}


# --- loading ----------------------------------------------------------------
def load_config_summaries(results_root: Path) -> dict[str, dict]:
    """Map ``config_name -> aggregated _summary.json`` for every results dir."""
    summaries: dict[str, dict] = {}
    if not results_root.exists():
        return summaries
    for summary_path in sorted(results_root.glob("*/_summary.json")):
        summaries[summary_path.parent.name] = json.loads(summary_path.read_text())
    return summaries


def load_config_trials(results_root: Path, config: str) -> list[dict]:
    """Per-trial JSON records for one config (excludes ``_summary.json``)."""
    cfg_dir = results_root / config
    if not cfg_dir.exists():
        return []
    return [
        json.loads(p.read_text())
        for p in sorted(cfg_dir.glob("*.json"))
        if p.name != "_summary.json"
    ]


def _median(stat: dict | None) -> float:
    return float(stat["median"]) if stat else 0.0


def _phases_present(summaries: dict[str, dict]) -> list[str]:
    seen = {ph for agg in summaries.values() for ph in agg.get("phase_wall_s", {})}
    ordered = [p for p in PHASE_ORDER if p in seen]
    return ordered + sorted(seen - set(ordered))


# --- figures ----------------------------------------------------------------
def fig_stacked_phase(summaries: dict[str, dict]) -> go.Figure:
    configs = list(summaries)
    fig = go.Figure()
    for phase in _phases_present(summaries):
        fig.add_bar(
            name=phase,
            x=configs,
            y=[
                _median(summaries[c].get("phase_wall_s", {}).get(phase))
                for c in configs
            ],
            marker_color=PHASE_COLORS.get(phase),
            hovertemplate="%{x}<br>" + phase + ": %{y:.1f}s<extra></extra>",
        )
    fig.update_layout(
        barmode="stack",
        title="Per-phase wall-clock (median) by config",
        xaxis_title="config",
        yaxis_title="seconds",
        legend_title="phase",
    )
    return fig


def fig_trial_distribution(trials: list[dict], config: str) -> go.Figure:
    totals = [
        r["summary"]["total_wall_s"]
        for r in trials
        if r.get("status") == "done" and r.get("summary")
    ]
    fig = go.Figure()
    fig.add_box(
        y=totals,
        name=config,
        boxpoints="all",
        jitter=0.5,
        pointpos=0,
        marker_color="#5B8FF9",
        hovertemplate="%{y:.1f}s<extra></extra>",
    )
    fig.update_layout(
        title=f"Per-trial total wall-clock distribution ({config})",
        yaxis_title="seconds",
    )
    return fig


def fig_model_calls(summaries: dict[str, dict]) -> go.Figure:
    configs = list(summaries)
    fig = go.Figure()
    for phase in _phases_present(summaries):
        ys = [_median(summaries[c].get("model_calls", {}).get(phase)) for c in configs]
        if not any(ys):
            continue
        fig.add_bar(name=phase, x=configs, y=ys, marker_color=PHASE_COLORS.get(phase))
    fig.update_layout(
        barmode="group",
        title="Model-call counts (median) by phase and config",
        xaxis_title="config",
        yaxis_title="model calls",
        legend_title="phase",
    )
    return fig


def fig_total_comparison(summaries: dict[str, dict]) -> go.Figure:
    configs = list(summaries)
    fig = go.Figure()
    fig.add_bar(
        name="total",
        x=configs,
        y=[_median(summaries[c].get("total_wall_s")) for c in configs],
        marker_color="#5B8FF9",
        hovertemplate="%{x}: %{y:.1f}s<extra></extra>",
    )
    for phase in _phases_present(summaries):
        fig.add_bar(
            name=phase,
            x=configs,
            y=[
                _median(summaries[c].get("phase_wall_s", {}).get(phase))
                for c in configs
            ],
            marker_color=PHASE_COLORS.get(phase),
            visible="legendonly",
        )
    fig.update_layout(
        barmode="group",
        title="Total wall-clock by config (toggle phases in legend)",
        xaxis_title="config",
        yaxis_title="seconds",
    )
    return fig


def fig_http_429_503(summaries: dict[str, dict]) -> go.Figure | None:
    configs = [c for c in summaries if summaries[c].get("http_429_503")]
    if not configs:
        return None
    fig = go.Figure()
    fig.add_bar(
        x=configs,
        y=[_median(summaries[c].get("http_429_503")) for c in configs],
        marker_color="#F4664A",
        hovertemplate="%{x}: %{y} entries<extra></extra>",
    )
    fig.update_layout(
        title="Quota-signal log entries (429/503, median) by config",
        xaxis_title="config",
        yaxis_title="log entries",
    )
    return fig


def build_figures(
    summaries: dict[str, dict], trials_by_config: dict[str, list[dict]]
) -> dict[str, go.Figure]:
    """Build every figure from loaded data. Pure (no I/O) so it's unit-testable."""
    figures: dict[str, go.Figure] = {}
    if not summaries:
        return figures
    figures["stacked_phase"] = fig_stacked_phase(summaries)
    figures["model_calls"] = fig_model_calls(summaries)
    figures["total_comparison"] = fig_total_comparison(summaries)
    # Baseline distribution if we have baseline trials, else the first config's.
    dist_config = (
        "baseline"
        if "baseline" in trials_by_config
        else next(iter(trials_by_config), None)
    )
    if dist_config and trials_by_config.get(dist_config):
        figures["trial_distribution"] = fig_trial_distribution(
            trials_by_config[dist_config], dist_config
        )
    http_fig = fig_http_429_503(summaries)
    if http_fig is not None:
        figures["http_429_503"] = http_fig
    return figures


# --- output -----------------------------------------------------------------
def write_report(figures: dict[str, go.Figure], report_path: Path) -> None:
    """Concatenate all figures into ONE self-contained interactive HTML file."""
    blocks = []
    for i, (name, fig) in enumerate(figures.items()):
        blocks.append(
            fig.to_html(
                full_html=False,
                include_plotlyjs="cdn" if i == 0 else False,
                div_id=name,
            )
        )
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>creative_agent latency report</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:24px;max-width:1100px}"
        "h1{font-size:20px}</style></head><body>"
        "<h1>creative_agent latency experiment</h1>"
        + "".join(blocks)
        + "</body></html>"
    )
    report_path.write_text(html)


def export_pngs(figures: dict[str, go.Figure], figures_dir: Path) -> list[str]:
    """Best-effort static PNG export (kaleido/Chrome). Returns names written."""
    figures_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, fig in figures.items():
        try:
            fig.write_image(str(figures_dir / f"{name}.png"), width=1000, height=560)
            written.append(name)
        except Exception as exc:  # noqa: BLE001 — PNG is optional; html is the deliverable
            print(f"  PNG export skipped for {name}: {type(exc).__name__}", flush=True)
    return written


def _write_figures_readme(figures_dir: Path, written: list[str]) -> None:
    lines = [
        "# Figures\n",
        "Static exports of `report.html` (regenerate with `plot.py`).\n",
    ]
    for name in written:
        lines.append(f"- `{name}.png`")
    (figures_dir / "README.md").write_text("\n".join(lines) + "\n")


def build_report(results_root: Path = RESULTS_ROOT) -> dict[str, go.Figure]:
    summaries = load_config_summaries(results_root)
    if not summaries:
        print(f"no results found under {results_root} — run run_experiment.py first")
        return {}
    trials_by_config = {c: load_config_trials(results_root, c) for c in summaries}
    figures = build_figures(summaries, trials_by_config)
    write_report(figures, REPORT_PATH)
    print(f"wrote {REPORT_PATH} ({len(figures)} figures)")
    written = export_pngs(figures, FIGURES_DIR)
    _write_figures_readme(FIGURES_DIR, written)
    if written:
        print(f"wrote {len(written)} PNG(s) to {FIGURES_DIR}")
    else:
        print("no PNGs written (kaleido/Chrome unavailable) — report.html is complete")
    return figures


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results-root", default=str(RESULTS_ROOT))
    args = p.parse_args(argv)
    build_report(Path(args.results_root))


if __name__ == "__main__":
    main()
