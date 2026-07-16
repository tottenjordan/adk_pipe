"""Offline tests for the matplotlib static-figure renderer (no Chrome, no network).

Unlike the Plotly PNG path (kaleido/Chrome, best-effort), matplotlib's Agg
backend renders headless anywhere, so these actually write and assert PNG files.
"""

import json

from experiments.creative_latency.render_static import (
    render_all,
    render_stacked_phases,
    render_totals_and_ratelimits,
)


def _summary(config, total, phases, http=None):
    def st(v):
        return {"median": v, "min": v, "max": v}

    return {
        "config": config,
        "n_trials": 3,
        "n_done": 3,
        "total_wall_s": st(total),
        "phase_wall_s": {k: st(v) for k, v in phases.items()},
        "http_429_503": st(http) if http is not None else None,
    }


SUMMARIES = {
    "baseline": _summary(
        "baseline", 380.0, {"research": 138.0, "visual": 102.0, "eval": 65.0}, http=0
    ),
    "fewer_pro": _summary(
        "fewer_pro", 324.0, {"research": 81.0, "visual": 100.0, "eval": 65.0}, http=0
    ),
    "parallel_img": _summary(
        "parallel_img", 400.0, {"research": 134.0, "visual": 90.0, "eval": 70.0}, http=1
    ),
}


def _write_results(root):
    for name, agg in SUMMARIES.items():
        cfg = root / name
        cfg.mkdir()
        (cfg / "_summary.json").write_text(json.dumps(agg))


def _is_png(path):
    return path.exists() and path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


class TestRenderStackedPhases:
    def test_writes_png(self, tmp_path):
        out = render_stacked_phases(SUMMARIES, tmp_path / "figures" / "phases.png")
        assert _is_png(out)


class TestRenderTotalsAndRateLimits:
    def test_writes_png(self, tmp_path):
        out = render_totals_and_ratelimits(
            SUMMARIES, tmp_path / "figures" / "totals.png"
        )
        assert _is_png(out)

    def test_handles_missing_http_field(self, tmp_path):
        # http_429_503 absent (None) must not raise — defaults to 0.
        summaries = {"baseline": _summary("baseline", 300.0, {"research": 300.0})}
        out = render_totals_and_ratelimits(summaries, tmp_path / "t.png")
        assert _is_png(out)


class TestRenderAll:
    def test_writes_both_figures_from_results_dir(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        _write_results(results)
        written = render_all(results, tmp_path / "figures")
        assert len(written) == 2
        assert all(_is_png(p) for p in written)

    def test_empty_results_writes_nothing(self, tmp_path):
        assert render_all(tmp_path / "nope", tmp_path / "figures") == []
