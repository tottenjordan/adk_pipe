"""Offline smoke tests for the Plotly report builder (no Chrome, no network).

These exercise the pure figure-building and the self-contained HTML writer.
PNG export (kaleido/Chrome) is intentionally NOT tested — it is best-effort.
"""

import plotly.graph_objects as go

from experiments.creative_latency.plot import (
    build_figures,
    load_config_summaries,
    write_report,
)


def _summary(config, total, phases, calls=None, http=None):
    def st(v):
        return {"median": v, "min": v, "max": v}

    return {
        "config": config,
        "n_trials": 3,
        "n_done": 3,
        "total_wall_s": st(total),
        "phase_wall_s": {k: st(v) for k, v in phases.items()},
        "model_calls": {k: st(v) for k, v in (calls or {}).items()},
        "http_429_503": st(http) if http is not None else None,
    }


SUMMARIES = {
    "baseline": _summary(
        "baseline",
        300.0,
        {"research": 180.0, "image_gen": 80.0, "eval": 40.0},
        calls={"research": 8, "eval": 12},
        http=15,
    ),
    "fewer_pro": _summary(
        "fewer_pro",
        240.0,
        {"research": 120.0, "image_gen": 80.0, "eval": 40.0},
        calls={"research": 5, "eval": 12},
        http=9,
    ),
}

TRIALS = {
    "baseline": [
        {"status": "done", "summary": {"total_wall_s": t}}
        for t in (290.0, 300.0, 320.0)
    ],
    "fewer_pro": [],
}


class TestBuildFigures:
    def test_core_figures_present(self):
        figs = build_figures(SUMMARIES, TRIALS)
        for name in ("stacked_phase", "model_calls", "total_comparison"):
            assert name in figs
            assert isinstance(figs[name], go.Figure)

    def test_distribution_uses_baseline(self):
        assert "trial_distribution" in build_figures(SUMMARIES, TRIALS)

    def test_http_figure_present_when_data(self):
        assert "http_429_503" in build_figures(SUMMARIES, TRIALS)

    def test_http_figure_absent_when_no_data(self):
        s = {"baseline": _summary("baseline", 100.0, {"research": 100.0}, http=None)}
        assert "http_429_503" not in build_figures(s, {"baseline": []})

    def test_empty_summaries_yields_no_figures(self):
        assert build_figures({}, {}) == {}


class TestWriteReport:
    def test_writes_self_contained_html(self, tmp_path):
        figs = build_figures(SUMMARIES, TRIALS)
        out = tmp_path / "report.html"
        write_report(figs, out)
        html = out.read_text()
        assert html.startswith("<!doctype html>")
        assert "creative_agent latency" in html
        # Plotly JS is pulled in exactly once (first figure, CDN).
        assert "plotly" in html.lower()


class TestLoadSummaries:
    def test_reads_summary_json(self, tmp_path):
        cfg = tmp_path / "baseline"
        cfg.mkdir()
        (cfg / "_summary.json").write_text('{"config": "baseline", "n_done": 3}')
        (cfg / "abc.json").write_text('{"session_id": "abc"}')  # per-trial, ignored
        loaded = load_config_summaries(tmp_path)
        assert loaded == {"baseline": {"config": "baseline", "n_done": 3}}

    def test_missing_root_is_empty(self, tmp_path):
        assert load_config_summaries(tmp_path / "nope") == {}
