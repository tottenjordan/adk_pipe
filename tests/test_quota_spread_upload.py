"""Pure-transform tests for the Agent Platform Experiments post-hoc uploader.

The ``aiplatform`` calls are live-only; here we pin the record→(name, params,
metrics) shaping, including quality harvest and Vertex-name sanitization.
"""

from __future__ import annotations

from experiments.quota_spread.upload_to_vertex import record_to_run


def _report(pass_rate: float, overall: float) -> dict:
    return {
        "summary": {"overall_pass_rate": pass_rate},
        "ad_copy_evaluations": [{"score": {"overall_score": overall}}],
        "visual_concept_evaluations": [{"score": {"overall_score": overall}}],
    }


def test_record_to_run_shapes_params_metrics_and_quality():
    record = {
        "arm": "global_3x",
        "concurrency": 5,
        "revision": "trend-trawler-api-00068-muz",
        "batch_id": "global_3x_N5_r1",
        "status": "done",
        "session_id": "abcd1234ef56",
        "research_s": 120.5,
        "visual_s": 60.0,
        "eval_s": 30.0,
        "total_s": 240.0,
        "count_429": 3,
        "state": {"creative_evaluation_report": _report(0.75, 0.82)},
    }
    name, params, metrics = record_to_run(record)

    # Vertex run names: lowercase alnum + hyphens only (no underscores).
    assert name == "global-3x-n5-global-3x-n5-r1-abcd1234"
    assert "_" not in name
    assert params == {
        "arm": "global_3x",
        "concurrency": 5,
        "revision": "trend-trawler-api-00068-muz",
        "batch_id": "global_3x_N5_r1",
        "status": "done",
    }
    assert metrics["research_s"] == 120.5
    assert metrics["total_s"] == 240.0
    assert metrics["count_429"] == 3.0
    assert metrics["eval_pass"] == 0.75
    assert metrics["eval_mean"] == 0.82


def test_record_to_run_drops_none_metrics_and_handles_error_record():
    record = {
        "arm": "regional_25",
        "concurrency": 1,
        "batch_id": "regional_25_N1_r0",
        "status": "error",
        "session_id": None,
        "research_s": None,
        "total_s": None,
        "count_429": None,
        "state": {},
    }
    name, params, metrics = record_to_run(record)

    assert name.startswith("regional-25-n1-")
    assert name.endswith("nosessio")  # sid fallback, truncated to 8 chars
    assert params["status"] == "error"
    assert "revision" not in params  # absent -> dropped
    assert metrics == {}  # nothing measured -> no metrics logged
