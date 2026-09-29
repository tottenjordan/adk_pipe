"""Tests for the adk eval efficiency regression gate (tests/eval/efficiency_gate.py).

Fixtures are built from ADK's real ``EvalSetResult`` / ``EvalCaseResult`` models
and dumped to JSON, so a field rename in ADK breaks these tests rather than the
nightly gate silently reading nothing.
"""

import json
import os

from google.adk.evaluation.eval_metrics import EvalMetricResult, EvalStatus
from google.adk.evaluation.eval_result import EvalCaseResult, EvalSetResult

from tests.eval import efficiency_gate as gate

BASE_METRICS = {
    "token_usage_v1": 10000.0,
    "inference_call_count_v1": 10.0,
    "tool_call_count_v1": 5.0,
    "invocation_duration_v1": 100.0,
}


def _case(eval_id, metrics, status=EvalStatus.PASSED, extra=None):
    results = [
        EvalMetricResult(
            metric_name=name, score=score, eval_status=EvalStatus.INFORMATIONAL
        )
        for name, score in metrics.items()
    ]
    for name, score, st in extra or []:
        results.append(EvalMetricResult(metric_name=name, score=score, eval_status=st))
    return EvalCaseResult(
        eval_set_id="set",
        eval_id=eval_id,
        final_eval_status=status,
        overall_eval_metric_results=results,
        eval_metric_result_per_invocation=[],
        session_id=f"s-{eval_id}",
    )


def _raw(*cases):
    return EvalSetResult(
        eval_set_result_id="r", eval_set_id="set", eval_case_results=list(cases)
    ).model_dump(mode="json")


def test_extract_reads_scores_and_passed():
    raw = _raw(
        _case(
            "c1",
            BASE_METRICS,
            extra=[("rubric_based_final_response_quality_v1", 0.9, EvalStatus.PASSED)],
        ),
        _case("c2", {"token_usage_v1": 1.0}, status=EvalStatus.FAILED),
    )
    out = gate.extract_metrics(raw)
    assert out["c1"]["token_usage_v1"] == 10000.0
    assert out["c1"]["invocation_duration_v1"] == 100.0
    assert out["c1"]["_passed"] is True
    # Only the efficiency metrics are extracted, not the quality rubrics.
    assert "rubric_based_final_response_quality_v1" not in out["c1"]
    assert out["c2"]["_passed"] is False


def test_extract_accepts_camel_case_json():
    raw = json.loads(
        EvalSetResult.model_validate(_raw(_case("c1", BASE_METRICS))).model_dump_json(
            by_alias=True
        )
    )
    assert gate.extract_metrics(raw)["c1"]["tool_call_count_v1"] == 5.0


def _current(eval_id="c1", passed=True, **overrides):
    return {eval_id: {**BASE_METRICS, **overrides, "_passed": passed}}


def test_token_regression_fails():
    failures, warnings = gate.compare(
        _current(token_usage_v1=13000.0), {"c1": BASE_METRICS}
    )
    assert len(failures) == 1 and "token_usage_v1" in failures[0]
    assert warnings == []


def test_count_regression_fails():
    failures, _ = gate.compare(
        _current(inference_call_count_v1=14.0, tool_call_count_v1=7.0),
        {"c1": BASE_METRICS},
    )
    assert len(failures) == 2


def test_duration_only_warns():
    failures, warnings = gate.compare(
        _current(invocation_duration_v1=200.0), {"c1": BASE_METRICS}
    )
    assert failures == []
    assert len(warnings) == 1 and "invocation_duration_v1" in warnings[0]


def test_within_tolerance_passes():
    failures, warnings = gate.compare(
        _current(
            token_usage_v1=12400.0,
            inference_call_count_v1=12.0,
            tool_call_count_v1=6.0,
            invocation_duration_v1=140.0,
        ),
        {"c1": BASE_METRICS},
    )
    assert failures == [] and warnings == []


def test_missing_baseline_is_not_a_failure():
    failures, warnings = gate.compare(_current(token_usage_v1=1e9), {})
    assert failures == []
    assert any("no baseline" in w for w in warnings)


def test_zero_baseline_does_not_divide_by_zero():
    base = {**BASE_METRICS, "tool_call_count_v1": 0.0}
    failures, warnings = gate.compare(_current(tool_call_count_v1=3.0), {"c1": base})
    assert failures == []
    assert any("tool_call_count_v1" in w for w in warnings)


def test_failed_case_fails_even_without_baseline():
    failures, _ = gate.compare(_current(passed=False), {})
    assert len(failures) == 1 and "c1" in failures[0]


def _write_history(tmp_path, raw):
    hist = tmp_path / "history"
    hist.mkdir()
    (hist / "old.evalset_result.json").write_text(json.dumps(_raw()))
    new = hist / "new.evalset_result.json"
    new.write_text(json.dumps(raw))
    # Make sure "new" is the newest by mtime.
    os.utime(hist / "old.evalset_result.json", (1, 1))
    return hist


def test_main_exits_1_on_failures(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    hist = _write_history(
        tmp_path, _raw(_case("c1", {**BASE_METRICS, "token_usage_v1": 20000.0}))
    )
    baseline = tmp_path / "b.json"
    baseline.write_text(json.dumps({"trend_scout": {"c1": BASE_METRICS}}))
    rc = gate.main(
        [
            "--agent",
            "trend_scout",
            "--baseline",
            str(baseline),
            "--history-dir",
            str(hist),
        ]
    )
    assert rc == 1


def test_main_exits_0_and_writes_step_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    summary.write_text("existing\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    hist = _write_history(tmp_path, _raw(_case("c1", BASE_METRICS)))
    baseline = tmp_path / "b.json"
    baseline.write_text(json.dumps({"trend_scout": {"c1": BASE_METRICS}}))
    rc = gate.main(
        [
            "--agent",
            "trend_scout",
            "--baseline",
            str(baseline),
            "--history-dir",
            str(hist),
        ]
    )
    assert rc == 0
    text = summary.read_text()
    assert text.startswith("existing\n")
    assert "| c1 | token_usage_v1 |" in text
    assert "| c1 | token_usage_v1 |" in capsys.readouterr().out


def test_update_baseline_writes_and_merges(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    hist = _write_history(tmp_path, _raw(_case("c1", BASE_METRICS)))
    baseline = tmp_path / "b.json"
    baseline.write_text(json.dumps({"creative_agent": {"x": {"token_usage_v1": 1}}}))
    rc = gate.main(
        [
            "--agent",
            "trend_scout",
            "--baseline",
            str(baseline),
            "--history-dir",
            str(hist),
            "--update-baseline",
        ]
    )
    assert rc == 0
    data = json.loads(baseline.read_text())
    assert data["creative_agent"] == {"x": {"token_usage_v1": 1}}
    assert data["trend_scout"] == {"c1": BASE_METRICS}


def test_update_baseline_refuses_failed_case(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    hist = _write_history(
        tmp_path, _raw(_case("c1", BASE_METRICS, status=EvalStatus.FAILED))
    )
    baseline = tmp_path / "b.json"
    baseline.write_text("{}")
    rc = gate.main(
        [
            "--agent",
            "trend_scout",
            "--baseline",
            str(baseline),
            "--history-dir",
            str(hist),
            "--update-baseline",
        ]
    )
    assert rc == 1
    assert json.loads(baseline.read_text()) == {}


def test_main_no_history_exits_2(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = gate.main(
        [
            "--agent",
            "trend_scout",
            "--baseline",
            str(tmp_path / "b.json"),
            "--history-dir",
            str(empty),
        ]
    )
    assert rc == 2


def test_main_missing_history_dir_exits_2(tmp_path):
    rc = gate.main(
        ["--agent", "a", "--baseline", "x", "--history-dir", str(tmp_path / "nope")]
    )
    assert rc == 2
