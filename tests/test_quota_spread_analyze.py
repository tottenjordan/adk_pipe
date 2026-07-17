"""Pure-core tests for the quota-spread analysis (slope + tidy CSV) and plots.

All offline: synthetic records, a tmp results tree, and PNG smoke — no network.
"""

import csv


def test_median_research_by_cell():
    from experiments.quota_spread.analyze import median_research_by_cell

    records = [
        {"arm": "A", "concurrency": 1, "status": "done", "research_s": 100.0},
        {"arm": "A", "concurrency": 1, "status": "done", "research_s": 110.0},
        {"arm": "A", "concurrency": 5, "status": "done", "research_s": 300.0},
        {"arm": "B", "concurrency": 1, "status": "done", "research_s": 100.0},
        {"arm": "B", "concurrency": 5, "status": "done", "research_s": 105.0},
        # error runs are excluded from the medians
        {"arm": "B", "concurrency": 5, "status": "error", "research_s": None},
    ]
    med = median_research_by_cell(records)
    assert med[("A", 1)] == 105.0
    assert med[("A", 5)] == 300.0
    assert med[("B", 1)] == 100.0
    assert med[("B", 5)] == 105.0


def test_research_slope_baseline_steeper_than_treatment():
    """H1 signal: the double-up baseline inflates with N; the spread stays flat."""
    from experiments.quota_spread.analyze import research_slope

    baseline = {1: 100.0, 5: 300.0}  # research time balloons with concurrency
    treatment = {1: 100.0, 5: 110.0}  # nearly flat
    assert research_slope(baseline) > research_slope(treatment)


def test_research_slope_single_point_is_zero():
    from experiments.quota_spread.analyze import research_slope

    assert research_slope({1: 100.0}) == 0.0
    assert research_slope({}) == 0.0


def test_to_tidy_rows_and_write_csv(tmp_path):
    from experiments.quota_spread.analyze import to_tidy_rows, write_csv

    records = [
        {
            "arm": "A",
            "concurrency": 1,
            "batch_id": "b1",
            "session_id": "s1",
            "status": "done",
            "research_s": 100.0,
            "total_s": 300.0,
            "count_429": 2,
            "exhaustion": [],
            "state": {},
        },
        {
            "arm": "A",
            "concurrency": 5,
            "batch_id": "b2",
            "session_id": "s2",
            "status": "done",
            "research_s": 200.0,
            "total_s": 359.0,
            "count_429": None,
            "exhaustion": ["campaign_web_search_insights__retry_exhausted"],
            "state": {},
        },
    ]
    rows = to_tidy_rows(records)
    assert len(rows) == len(records)
    assert rows[0]["arm"] == "A"
    assert rows[0]["N"] == 1
    assert rows[0]["research_s"] == 100.0

    out = write_csv(rows, tmp_path / "runs.csv")
    with open(out) as f:
        got = list(csv.DictReader(f))
    assert len(got) == 2
    assert {
        "arm",
        "N",
        "batch",
        "session",
        "research_s",
        "total_s",
        "eval_pass",
        "eval_mean",
        "count_429",
        "exhaustion",
    } <= set(got[0].keys())


def test_load_records_skips_manifest(tmp_path):
    import json

    from experiments.quota_spread.analyze import load_records

    (tmp_path / "A" / "N1" / "b1").mkdir(parents=True)
    (tmp_path / "A" / "N1" / "b1" / "s1.json").write_text(
        json.dumps({"arm": "A", "concurrency": 1, "status": "done", "research_s": 1.0})
    )
    (tmp_path / "manifest.json").write_text(json.dumps({"cells": []}))
    recs = load_records(tmp_path)
    assert len(recs) == 1
    assert recs[0]["arm"] == "A"


def _sample_eval_state():
    """A state dict shaped like the real creative_evaluation_report model_dump()."""
    return {
        "creative_evaluation_report": {
            "ad_copy_evaluations": [
                {
                    "score": {
                        "overall_score": 0.8,
                        "passed": True,
                        "verdicts": [
                            {"dimension": "copy_quality", "score": 9, "verdict": "pass"},
                            {"dimension": "audience_fit", "score": 7, "verdict": "pass"},
                        ],
                    }
                },
                {
                    "score": {
                        "overall_score": 0.6,
                        "passed": False,
                        "verdicts": [
                            {"dimension": "copy_quality", "score": 5, "verdict": "fail"}
                        ],
                    }
                },
            ],
            "visual_concept_evaluations": [
                {
                    "score": {
                        "overall_score": 0.7,
                        "passed": True,
                        "verdicts": [
                            {"dimension": "aesthetics", "score": 7, "verdict": "pass"}
                        ],
                    }
                }
            ],
            "summary": {
                "overall_pass_rate": 0.667,
                "avg_ad_copy_score": 0.7,
                "avg_visual_score": 0.7,
            },
            "warnings": [],
        }
    }


def test_extract_quality_from_state():
    from experiments.quota_spread.quality import extract_quality

    q = extract_quality(_sample_eval_state())
    assert q is not None
    assert q["pass_rate"] == 0.667
    # grand mean of overall_score across all 3 creatives
    assert abs(q["mean_score"] - (0.8 + 0.6 + 0.7) / 3) < 1e-9
    assert q["n_creatives"] == 3
    # per-dimension mean of raw 1–10 verdict scores
    assert abs(q["dims"]["copy_quality"] - 7.0) < 1e-9  # (9+5)/2
    assert abs(q["dims"]["audience_fit"] - 7.0) < 1e-9
    assert q["warnings"] == []


def test_extract_quality_missing_key_returns_none():
    from experiments.quota_spread.quality import extract_quality

    assert extract_quality({}) is None
    assert extract_quality({"other": 1}) is None


def test_extract_quality_empty_report_degrades():
    """A present-but-empty report must not crash — metrics degrade to None."""
    from experiments.quota_spread.quality import extract_quality

    q = extract_quality({"creative_evaluation_report": {}})
    assert q is not None
    assert q["pass_rate"] is None
    assert q["mean_score"] is None
    assert q["n_creatives"] == 0


def test_quality_by_cell_averages_floats():
    """Per-run pass-rate/mean-score floats are averaged across a cell (not bool-counted)."""
    from experiments.quota_spread.analyze import quality_by_cell
    from experiments.quota_spread.quality import extract_quality

    def qf(state):
        q = extract_quality(state)
        return (q["pass_rate"], q["mean_score"]) if q else (None, None)

    records = [
        {"arm": "A", "concurrency": 5, "status": "done", "state": _sample_eval_state()},
        {"arm": "A", "concurrency": 5, "status": "done", "state": _sample_eval_state()},
    ]
    cells = quality_by_cell(records, qf)
    assert abs(cells["A|5"]["pass_rate"] - 0.667) < 1e-9
    assert cells["A|5"]["n_scored"] == 2


def test_doe_plot_render_all_writes_pngs(tmp_path):
    """Smoke: render_all produces the three figures from synthetic records."""
    from experiments.quota_spread.doe_plot import render_all

    records = [
        {"arm": "global_3x", "concurrency": 1, "status": "done",
         "research_s": 100.0, "total_s": 300.0, "state": {}},
        {"arm": "global_3x", "concurrency": 5, "status": "done",
         "research_s": 260.0, "total_s": 480.0, "state": {}},
        {"arm": "regional_25", "concurrency": 1, "status": "done",
         "research_s": 100.0, "total_s": 300.0, "state": {}},
        {"arm": "regional_25", "concurrency": 5, "status": "done",
         "research_s": 120.0, "total_s": 330.0, "state": {}},
    ]
    paths = render_all(records, figures_dir=tmp_path)
    assert len(paths) == 3
    for p in paths:
        assert p.exists()
        assert p.suffix == ".png"
        assert p.stat().st_size > 0
