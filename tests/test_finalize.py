"""Unit tests for creative_agent.finalize (finalize_pipeline's function nodes)."""

import asyncio
import threading
from typing import Any

import pytest
from google.api_core import exceptions as gexc

from creative_agent import finalize as fin
from tests._fake_bq import FakeBigQueryClient
from tests._fakes import FakeToolContext

# The real tool, captured before any test monkeypatches it.
_REAL_WRITE_EVAL = fin.bq_tools.write_eval_report_to_bq

_REPORT = {
    "summary": {
        "total_ad_copies": 4,
        "ad_copies_passed": 3,
        "avg_ad_copy_score": 0.812,
        "total_visual_concepts": 4,
        "visual_concepts_passed": 3,
        "avg_visual_score": 0.77,
        "overall_pass_rate": 0.75,
        "weakest_dimensions": ["trend_connection", "copy_quality"],
    },
    "ad_copy_evaluations": [
        {"headline": "Fast Feet", "score": {"overall_score": 0.62, "passed": False}},
        {"headline": "Zoom", "score": {"overall_score": 0.9, "passed": True}},
    ],
    "visual_concept_evaluations": [
        {"concept_name": "Dust Cloud", "score": {"overall_score": 0.6, "passed": False}}
    ],
}


# --- finalize_summary (pure) --- #


def test_summary_reports_scores_counts_and_uris():
    out = fin.finalize_summary(
        {
            "creative_evaluation_report": _REPORT,
            "eval_report_gcs_uri": "gs://b/eval.json",
            "creative_gallery_gcs_uri": "gs://b/gallery.html",
            "research_report_gcs_uri": "gs://b/report.pdf",
        }
    )
    assert "6/8 creatives passed" in out and "75%" in out
    assert "0.81" in out and "0.77" in out
    assert "4 ad copies" in out and "4 visual concepts" in out
    assert "Trend connection" in out  # readable dimension labels
    assert "'Fast Feet' (ad copy, 0.62)" in out and "'Dust Cloud' (visual" in out
    assert "'Zoom'" not in out
    for uri in ("gs://b/eval.json", "gs://b/gallery.html", "gs://b/report.pdf"):
        assert uri in out
    assert "Failed steps" not in out
    assert len(out) < 1000


def test_summary_without_a_report_is_a_non_empty_notice():
    for state in ({}, {"creative_evaluation_report": None}):
        out = fin.finalize_summary(state)
        assert out.strip() and "creative_evaluation_report" in out


def test_summary_names_failed_steps():
    out = fin.finalize_summary(
        {
            "creative_evaluation_report__retry_exhausted": True,
            "creative_gallery_gcs_uri__issues": "HTML gallery failed: boom",
            "eval_bq_row_uuid__issues": "eval row (BigQuery) failed: x",
        }
    )
    assert "Failed steps: evaluation, HTML gallery, eval row (BigQuery)." in out


def test_issue_message_is_short_and_single_line():
    msg = fin.issue_message("HTML gallery", RuntimeError("line1\nline2 " + "x" * 500))
    assert msg.startswith("HTML gallery failed: RuntimeError: line1 line2")
    assert "\n" not in msg and len(msg) <= 200


# --- evaluate_creatives_node --- #


def test_evaluate_runs_the_judge_off_loop_on_a_snapshot(monkeypatch):
    ctx = FakeToolContext({"ad_copy_critique": {"ad_copies": [{"id": 1}]}})
    seen: dict[str, Any] = {}

    def fake_eval(holder):
        seen["thread"] = threading.current_thread()
        seen["same_state"] = holder.state is ctx.state
        holder.state["creative_evaluation_report"] = _REPORT
        return {"status": "success"}

    monkeypatch.setattr(fin.eval_agent, "evaluate_all_creatives", fake_eval)
    asyncio.run(fin.evaluate_creatives_node(ctx))  # ty: ignore[invalid-argument-type]
    assert seen["thread"] is not threading.main_thread()
    assert seen["same_state"] is False
    assert ctx.state["creative_evaluation_report"] == _REPORT
    assert "creative_evaluation_report__retry_exhausted" not in ctx.state


@pytest.mark.parametrize(
    "fake",
    [
        lambda holder: {"status": "error", "message": "No ad copies"},
        lambda holder: (_ for _ in ()).throw(RuntimeError("judge down")),
    ],
    ids=["no-creatives", "judge-raises"],
)
def test_evaluate_failure_records_marker_and_never_raises(monkeypatch, fake):
    ctx = FakeToolContext({})
    monkeypatch.setattr(fin.eval_agent, "evaluate_all_creatives", fake)
    asyncio.run(fin.evaluate_creatives_node(ctx))  # ty: ignore[invalid-argument-type]
    assert ctx.state["creative_evaluation_report__retry_exhausted"] is True
    assert "creative_evaluation_report" not in ctx.state


# --- persist_node --- #

_CREATIVES = {
    "final_visual_concepts": {"visual_concepts": [{"concept_name": "c"}]},
    "ad_copy_critique": {"ad_copies": [{"id": 1}]},
}


def _patch_steps(monkeypatch, calls: list[str], **overrides):
    """Replace the four persistence tools with recorders (or ``overrides``)."""

    def eval_gcs(ctx):
        calls.append("eval_gcs")
        ctx.state["eval_report_gcs_uri"] = "gs://b/eval.json"
        return {"status": "success"}

    async def gallery(ctx):
        calls.append("gallery")
        return {"status": "success", "gcs_uri": "gs://b/gallery.html"}

    def trends(ctx):
        calls.append("trends")
        ctx.state["creative_row_uuid"] = "row1"
        return {"status": "success"}

    def eval_bq(ctx):
        calls.append("eval_bq")
        ctx.state["eval_bq_row_uuid"] = "eval1"
        return {"status": "success"}

    steps = {
        "eval_gcs": eval_gcs,
        "gallery": gallery,
        "trends": trends,
        "eval_bq": eval_bq,
    } | overrides
    monkeypatch.setattr(fin.gcs_tools, "save_eval_report_to_gcs", steps["eval_gcs"])
    monkeypatch.setattr(fin.tools, "save_creative_gallery_html", steps["gallery"])
    monkeypatch.setattr(fin.bq_tools, "write_trends_to_bq", steps["trends"])
    monkeypatch.setattr(fin.bq_tools, "write_eval_report_to_bq", steps["eval_bq"])


def _persist(state: dict[str, Any]) -> FakeToolContext:
    ctx = FakeToolContext(state)
    asyncio.run(fin.persist_node(ctx))  # ty: ignore[invalid-argument-type]
    return ctx


def test_persist_runs_every_step_in_order(monkeypatch):
    calls: list[str] = []
    _patch_steps(monkeypatch, calls)
    ctx = _persist({"creative_evaluation_report": _REPORT, **_CREATIVES})
    assert calls == ["eval_gcs", "gallery", "trends", "eval_bq"]
    assert ctx.state["creative_gallery_gcs_uri"] == "gs://b/gallery.html"
    assert ctx.state["eval_bq_row_uuid"] == "eval1"
    assert not [k for k in ctx.state if k.endswith("__issues")]


def test_persist_isolates_a_raising_gallery(monkeypatch):
    calls: list[str] = []

    async def boom(ctx):
        calls.append("gallery")
        raise RuntimeError("gcs down")

    _patch_steps(monkeypatch, calls, gallery=boom)
    ctx = _persist({"creative_evaluation_report": _REPORT, **_CREATIVES})
    assert calls == ["eval_gcs", "gallery", "trends", "eval_bq"]
    assert "gcs down" in ctx.state["creative_gallery_gcs_uri__issues"]
    assert "creative_gallery_gcs_uri" not in ctx.state
    assert ctx.state["eval_bq_row_uuid"] == "eval1"


def test_persist_records_error_dicts(monkeypatch):
    calls: list[str] = []
    _patch_steps(
        monkeypatch,
        calls,
        eval_bq=lambda ctx: {"status": "error", "message": "no report"},
    )
    ctx = _persist({"creative_evaluation_report": _REPORT, **_CREATIVES})
    assert ctx.state["eval_bq_row_uuid__issues"] == (
        "eval row (BigQuery) failed: no report"
    )


def test_persist_isolates_a_raising_bq_row(monkeypatch):
    calls: list[str] = []

    def boom(ctx):
        raise RuntimeError("bq down")

    _patch_steps(monkeypatch, calls, trends=boom)
    ctx = _persist({"creative_evaluation_report": _REPORT, **_CREATIVES})
    assert "bq down" in ctx.state["creative_row_uuid__issues"]
    assert calls == ["eval_gcs", "gallery", "eval_bq"]


def test_persist_without_a_report_skips_the_eval_writes(monkeypatch):
    calls: list[str] = []
    _patch_steps(monkeypatch, calls)
    ctx = _persist({**_CREATIVES})
    assert calls == ["gallery", "trends"]
    assert "eval_report_gcs_uri__issues" not in ctx.state


def test_persist_without_creatives_skips_the_gallery(monkeypatch):
    calls: list[str] = []
    _patch_steps(monkeypatch, calls)
    ctx = _persist({})
    assert calls == ["trends"]
    assert "skipped" in ctx.state["creative_gallery_gcs_uri__issues"]


# --- review follow-ups --- #


def test_finalize_ready_writes_the_completion_marker():
    """runserver's auto-continue keys creative apps on finalize_done, so the
    terminal must set it on every path (incl. no report / failed eval write)."""
    for state in ({}, {"creative_evaluation_report": _REPORT}):
        ctx = FakeToolContext(state)
        out = fin.finalize_ready(ctx)  # ty: ignore[invalid-argument-type]
        assert out.strip()
        assert ctx.state["finalize_done"] is True


def test_evaluate_error_with_a_stale_report_clears_it(monkeypatch):
    """A judge error must not leave a previous run's report for persist_node."""
    ctx = FakeToolContext({"creative_evaluation_report": _REPORT})
    monkeypatch.setattr(
        fin.eval_agent,
        "evaluate_all_creatives",
        lambda holder: {"status": "error", "message": "No ad copies"},
    )
    asyncio.run(fin.evaluate_creatives_node(ctx))  # ty: ignore[invalid-argument-type]
    assert ctx.state["creative_evaluation_report__retry_exhausted"] is True
    assert not ctx.state.get("creative_evaluation_report")


def test_evaluate_success_clears_a_stale_exhausted_marker(monkeypatch):
    ctx = FakeToolContext({"creative_evaluation_report__retry_exhausted": True})

    def fake_eval(holder):
        holder.state["creative_evaluation_report"] = _REPORT
        return {"status": "success"}

    monkeypatch.setattr(fin.eval_agent, "evaluate_all_creatives", fake_eval)
    asyncio.run(fin.evaluate_creatives_node(ctx))  # ty: ignore[invalid-argument-type]
    assert ctx.state["creative_evaluation_report"] == _REPORT
    assert not ctx.state["creative_evaluation_report__retry_exhausted"]


def test_persist_skips_eval_writes_when_evaluation_exhausted(monkeypatch):
    """Even if a stale report survives, an exhausted evaluation skips eval writes."""
    calls: list[str] = []
    _patch_steps(monkeypatch, calls)
    _persist(
        {
            "creative_evaluation_report": _REPORT,
            "creative_evaluation_report__retry_exhausted": True,
            **_CREATIVES,
        }
    )
    assert calls == ["gallery", "trends"]


def test_judge_error_with_stale_report_end_to_end(monkeypatch):
    calls: list[str] = []
    _patch_steps(monkeypatch, calls)
    monkeypatch.setattr(
        fin.eval_agent,
        "evaluate_all_creatives",
        lambda holder: {"status": "error", "message": "No ad copies"},
    )
    ctx = FakeToolContext({"creative_evaluation_report": _REPORT, **_CREATIVES})
    asyncio.run(fin.evaluate_creatives_node(ctx))  # ty: ignore[invalid-argument-type]
    asyncio.run(fin.persist_node(ctx))  # ty: ignore[invalid-argument-type]
    assert calls == ["gallery", "trends"]
    assert "creative evaluation did not produce" in fin.finalize_summary(ctx.state)


def test_failed_trends_step_clears_the_creative_row_link(monkeypatch):
    """write_trends_to_bq sets creative_row_uuid before its MERGE; when the MERGE
    fails the eval row must not link to a row that was never written."""
    calls: list[str] = []
    seen: dict[str, Any] = {}

    def trends(ctx):
        calls.append("trends")
        ctx.state["creative_row_uuid"] = "row1"
        raise RuntimeError("bq down")

    def eval_bq(ctx):
        calls.append("eval_bq")
        seen["link"] = ctx.state.get("creative_row_uuid", "")
        return {"status": "success"}

    _patch_steps(monkeypatch, calls, trends=trends, eval_bq=eval_bq)
    monkeypatch.setattr(fin, "_RETRY_DELAYS", ())
    ctx = _persist({"creative_evaluation_report": _REPORT, **_CREATIVES})
    assert "bq down" in ctx.state["creative_row_uuid__issues"]
    assert calls[-1] == "eval_bq"
    assert not seen["link"]


def test_eval_row_written_with_empty_uri_when_gcs_save_fails(monkeypatch):
    """The real write_eval_report_to_bq still writes the scores, with an empty
    eval_report_gcs_uri link, when save_eval_report_to_gcs raised."""
    calls: list[str] = []

    def eval_gcs(ctx):
        calls.append("eval_gcs")
        raise ValueError("bad bucket")

    bq = FakeBigQueryClient()
    monkeypatch.setattr(fin.bq_tools, "_get_bigquery_client", lambda: bq)
    _patch_steps(monkeypatch, calls, eval_gcs=eval_gcs, eval_bq=_REAL_WRITE_EVAL)
    ctx = _persist({"creative_evaluation_report": _REPORT, **_CREATIVES})
    assert "bad bucket" in ctx.state["eval_report_gcs_uri__issues"]
    assert ctx.state["eval_bq_row_uuid"]
    (_, job_config), *_ = bq.queries
    params = {p.name: p.value for p in job_config.query_parameters}
    assert params["eval_report_gcs_uri"] == ""
    assert params["total_ad_copies"] == 4


# --- _run_step transient retry --- #


def _run_step(monkeypatch, step) -> tuple[FakeToolContext, list[float]]:
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(fin, "_sleep", fake_sleep)
    ctx = FakeToolContext({})
    asyncio.run(fin._run_step(ctx, fin.GALLERY_KEY, step))  # ty: ignore[invalid-argument-type]
    return ctx, slept


def _flaky(exc_factory, fail_times: int):
    attempts: list[int] = []

    def step(ctx):
        attempts.append(1)
        if len(attempts) <= fail_times:
            raise exc_factory()
        return {"status": "success"}

    return step, attempts


def test_run_step_retries_a_transient_error_then_succeeds(monkeypatch):
    step, attempts = _flaky(lambda: gexc.ServiceUnavailable("503"), 1)
    ctx, slept = _run_step(monkeypatch, step)
    assert len(attempts) == 2
    assert slept == [fin._RETRY_DELAYS[0]]
    assert "creative_gallery_gcs_uri__issues" not in ctx.state


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: gexc.TooManyRequests("429"),
        lambda: ConnectionError("reset"),
        lambda: TimeoutError("slow"),
    ],
    ids=["429", "connection", "timeout"],
)
def test_run_step_gives_up_after_three_transient_failures(monkeypatch, exc_factory):
    step, attempts = _flaky(exc_factory, 99)
    ctx, slept = _run_step(monkeypatch, step)
    assert len(attempts) == 3
    assert slept == list(fin._RETRY_DELAYS)
    assert "creative_gallery_gcs_uri__issues" in ctx.state


def test_run_step_does_not_retry_non_transient_errors_or_error_dicts(monkeypatch):
    step, attempts = _flaky(lambda: ValueError("bad"), 99)
    ctx, slept = _run_step(monkeypatch, step)
    assert len(attempts) == 1 and slept == []
    assert "bad" in ctx.state["creative_gallery_gcs_uri__issues"]

    calls: list[int] = []

    def error_dict(ctx):
        calls.append(1)
        return {"status": "error", "message": "nope"}

    ctx, slept = _run_step(monkeypatch, error_dict)
    assert len(calls) == 1 and slept == []
    assert "nope" in ctx.state["creative_gallery_gcs_uri__issues"]
