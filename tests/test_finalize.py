"""Unit tests for creative_agent.finalize (finalize_pipeline's function nodes)."""

import asyncio
import threading
from typing import Any

import pytest

from creative_agent import finalize as fin
from tests._fakes import FakeToolContext

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
