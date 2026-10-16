"""Tests for the adk eval efficiency regression gate (tests/eval/efficiency_gate.py).

Fixtures are built from ADK's real ``EvalSetResult`` / ``EvalCaseResult`` models
and dumped to JSON, so a field rename in ADK breaks these tests rather than the
nightly gate silently reading nothing.
"""

import json
import os
from pathlib import Path

from google.adk.evaluation.eval_case import (
    Invocation,
    InvocationEvent,
    InvocationEvents,
)
from google.adk.evaluation.eval_metrics import (
    EvalMetricResult,
    EvalMetricResultPerInvocation,
    EvalStatus,
)
from google.adk.evaluation.eval_result import EvalCaseResult, EvalSetResult
from google.genai import types

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
    # Trace-less fixture: an agent without a ToolSpec (deterministic check skipped).
    baseline.write_text(json.dumps({"demo_agent": {"c1": BASE_METRICS}}))
    rc = gate.main(
        [
            "--agent",
            "demo_agent",
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
            "demo_agent",
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
    assert data["demo_agent"] == {"c1": BASE_METRICS}


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


def test_main_default_history_dir_is_under_agents(tmp_path, monkeypatch, capsys):
    # `adk eval agents/<agent>` writes agents/<agent>/.adk/eval_history.
    monkeypatch.chdir(tmp_path)
    assert gate.main(["--agent", "a", "--baseline", "x"]) == 2
    assert str(Path("agents/a/.adk/eval_history")) in capsys.readouterr().err


# --- Deterministic tool-use check ------------------------------------------
# Fixtures mirror the real nightly evalset_result.json: root-agent calls are
# authored "root_agent" (creative_agent) / "trend_scout"; sub-agent calls made
# inside creative_pipeline (e.g. visual_generator's generate_image) carry their
# own author and must not count as root calls.

TOOL_USE = gate.TOOL_USE_METRIC
RESPONSE = "rubric_based_final_response_quality_v1"
CREATIVE_TRACE = [
    *[("root_agent", "call", "memorize")] * 5,
    *[("root_agent", "resp", "memorize")] * 5,
    ("root_agent", "call", "creative_pipeline"),
    ("visual_generator", "call", "generate_image"),
    ("visual_generator", "resp", "generate_image"),
    ("root_agent", "resp", "creative_pipeline"),
]
SCOUT_TOOLS = [
    "memorize",
    "gather_trends_agent",
    "understand_trends_agent_resilient",
    "pick_trends_agent",
    "save_search_trends_to_session_state",
    "write_trends_to_bq",
    "write_to_file",
    "save_session_state_to_gcs",
]


def _per_invocation(trace):
    events = []
    for author, kind, name in trace:
        part = (
            types.Part(function_call=types.FunctionCall(name=name, args={}))
            if kind == "call"
            else types.Part(
                function_response=types.FunctionResponse(name=name, response={})
            )
        )
        events.append(
            InvocationEvent(
                author=author, content=types.Content(role="model", parts=[part])
            )
        )
    return EvalMetricResultPerInvocation(
        actual_invocation=Invocation(
            user_content=types.Content(role="user", parts=[types.Part(text="go")]),
            intermediate_data=InvocationEvents(invocation_events=events),
        )
    )


def _traced_case(
    eval_id="c1",
    trace=None,
    tool_use=(0.0, EvalStatus.FAILED),
    response=(1.0, EvalStatus.PASSED),
):
    trace = CREATIVE_TRACE if trace is None else trace
    results = [
        EvalMetricResult(metric_name=n, score=s, eval_status=EvalStatus.INFORMATIONAL)
        for n, s in BASE_METRICS.items()
    ]
    for name, (score, status) in ((RESPONSE, response), (TOOL_USE, tool_use)):
        results.append(
            EvalMetricResult(metric_name=name, score=score, eval_status=status)
        )
    failed = EvalStatus.FAILED in {response[1], tool_use[1]}
    return EvalCaseResult(
        eval_set_id="set",
        eval_id=eval_id,
        final_eval_status=EvalStatus.FAILED if failed else EvalStatus.PASSED,
        overall_eval_metric_results=results,
        eval_metric_result_per_invocation=[_per_invocation(trace)],
        session_id=f"s-{eval_id}",
    )


def _extract(case, agent="creative_agent"):
    return gate.extract_metrics(_raw(case), agent=agent)["c1"]


def test_tool_specs_match_real_root_agents():
    # The gate imports nothing from the repo, so pin its hardcoded table to the
    # real root agents here: a renamed/added root tool must update TOOL_SPECS.
    import asyncio

    from creative_agent.agent import root_agent as creative_root
    from trend_scout.agent import root_agent as scout_root

    for agent, root in (("creative_agent", creative_root), ("trend_scout", scout_root)):
        spec = gate.TOOL_SPECS[agent]
        names = {t.name for t in asyncio.run(root.canonical_tools())}
        assert spec.root_author == root.name
        assert spec.allowed == names
        assert spec.required <= names
    config = Path(__file__).parent / "eval" / "eval_config.json"
    rubric = json.loads(config.read_text())["criteria"][TOOL_USE]["rubrics"]
    all_tools = next(r for r in rubric if r["rubric_id"] == "all_tools_used")
    text = all_tools["rubric_content"]["text_property"]
    listed = {t.strip(" .") for t in text.split(":", 1)[1].split(",")}
    assert gate.TOOL_SPECS["trend_scout"].required == listed


def test_root_tool_calls_ignore_sub_agents():
    case = EvalCaseResult.model_validate(_traced_case().model_dump(mode="json"))
    assert gate.root_tool_calls(case, "root_agent") == [
        *["memorize"] * 5,
        "creative_pipeline",
    ]


def test_judge_misread_with_correct_trace_passes():
    out = _extract(_traced_case(tool_use=(0.0, EvalStatus.FAILED)))
    assert out["_passed"] is True
    failures, warnings = gate.compare({"c1": out}, {"c1": BASE_METRICS})
    assert failures == []
    assert any("disagrees" in w and TOOL_USE in w for w in warnings)


def test_extra_root_call_fails():
    trace = [*CREATIVE_TRACE[:-1], ("root_agent", "call", "generate_image")]
    out = _extract(_traced_case(trace=trace, tool_use=(1.0, EvalStatus.PASSED)))
    assert out["_passed"] is False
    failures, _ = gate.compare({"c1": out}, {"c1": BASE_METRICS})
    assert any("generate_image" in f for f in failures)


def test_pipeline_called_twice_fails():
    trace = [*CREATIVE_TRACE, ("root_agent", "call", "creative_pipeline")]
    out = _extract(_traced_case(trace=trace, tool_use=(1.0, EvalStatus.PASSED)))
    assert out["_passed"] is False
    assert any("exactly once" in r for r in out["_reasons"])


def test_pipeline_not_last_fails():
    trace = [*CREATIVE_TRACE, ("root_agent", "call", "memorize")]
    out = _extract(_traced_case(trace=trace, tool_use=(1.0, EvalStatus.PASSED)))
    assert out["_passed"] is False
    assert any("last" in r for r in out["_reasons"])


def test_pipeline_missing_fails():
    trace = [("root_agent", "call", "memorize")]
    out = _extract(_traced_case(trace=trace, tool_use=(1.0, EvalStatus.PASSED)))
    assert out["_passed"] is False


def test_no_trace_fails_for_known_agent():
    out = _extract(_traced_case(trace=[], tool_use=(1.0, EvalStatus.PASSED)))
    assert out["_passed"] is False


def test_response_rubric_failed_fails():
    out = _extract(
        _traced_case(
            tool_use=(1.0, EvalStatus.PASSED), response=(0.5, EvalStatus.FAILED)
        )
    )
    assert out["_passed"] is False
    assert any(RESPONSE in r for r in out["_reasons"])


def test_trend_scout_full_trajectory_passes():
    trace = [("trend_scout", "call", t) for t in SCOUT_TOOLS]
    trace.append(("understand_trends_searcher", "call", "google_search"))
    out = _extract(_traced_case(trace=trace), agent="trend_scout")
    assert out["_passed"] is True


def test_trend_scout_missing_required_tool_fails():
    trace = [("trend_scout", "call", t) for t in SCOUT_TOOLS if t != "write_to_file"]
    out = _extract(
        _traced_case(trace=trace, tool_use=(1.0, EvalStatus.PASSED)),
        agent="trend_scout",
    )
    assert out["_passed"] is False
    assert any("write_to_file" in r for r in out["_reasons"])


def test_trend_scout_unknown_root_tool_fails():
    trace = [("trend_scout", "call", t) for t in [*SCOUT_TOOLS, "google_search"]]
    out = _extract(
        _traced_case(trace=trace, tool_use=(1.0, EvalStatus.PASSED)),
        agent="trend_scout",
    )
    assert out["_passed"] is False


def test_unknown_agent_skips_check_and_keeps_tool_rubric_gating():
    out = _extract(_traced_case(tool_use=(0.0, EvalStatus.FAILED)), agent="other")
    assert out["_passed"] is False
    assert out["_tool_check"] == "skipped"
    ok = _extract(_traced_case(tool_use=(1.0, EvalStatus.PASSED)), agent="other")
    assert ok["_passed"] is True


def _run_main(tmp_path, raw, baseline_data, *extra):
    hist = _write_history(tmp_path, raw)
    baseline = tmp_path / "b.json"
    baseline.write_text(json.dumps(baseline_data))
    rc = gate.main(
        [
            "--agent",
            "creative_agent",
            "--baseline",
            str(baseline),
            "--history-dir",
            str(hist),
            *extra,
        ]
    )
    return rc, json.loads(baseline.read_text())


def test_markdown_reports_tool_rubric_as_advisory(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rc, _ = _run_main(
        tmp_path, _raw(_traced_case()), {"creative_agent": {"c1": BASE_METRICS}}
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert f"| c1 | {TOOL_USE} |" in out and "advisory" in out
    assert "| c1 | tool_use_check |" in out


def test_update_baseline_accepts_judge_misread(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rc, data = _run_main(tmp_path, _raw(_traced_case()), {}, "--update-baseline")
    assert rc == 0
    assert data == {"creative_agent": {"c1": BASE_METRICS}}


def test_update_baseline_refuses_bad_trace(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    trace = [*CREATIVE_TRACE, ("root_agent", "call", "creative_pipeline")]
    raw = _raw(_traced_case(trace=trace, tool_use=(1.0, EvalStatus.PASSED)))
    rc, data = _run_main(tmp_path, raw, {}, "--update-baseline")
    assert rc == 1
    assert data == {}
