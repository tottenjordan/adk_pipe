"""Early ad-copy evaluation: judge ad copies while the visuals render.

``creative_pipeline`` fans out after ``ad_creative_pipeline``: one branch runs
the visual stage, the other (``finalize.evaluate_ad_copies_node`` →
``creative_eval.agent.evaluate_ad_copies_only``) judges the ad copies and stores
them in ``ad_copy_evaluations_partial``. ``evaluate_all_creatives`` (finalize)
then judges only the visuals and merges the stored ad-copy results into the
same report. Without a usable partial it evaluates everything, as before.
"""

import asyncio
import copy
import threading
from typing import Any

import pytest

from creative_eval import agent as ev_agent
from creative_eval.schemas import (
    AdCopyEvaluation,
    CreativeScore,
    EvalVerdict,
    VisualConceptEvaluation,
)
from tests._fakes import FakeToolContext

_CAMPAIGN = {
    "brand": "Acme",
    "target_product": "Widget",
    "target_audience": "Makers",
    "key_selling_points": "Fast",
    "target_search_trends": "trend",
}
_ADS = {
    "ad_copies": [
        {"original_id": i, "headline": f"H{i}", "tone_style": "Bold"}
        for i in range(1, 5)
    ]
}
_VISUALS = {
    "visual_concepts": [{"ad_copy_id": i, "concept_name": f"C{i}"} for i in range(1, 5)]
}


def _score(overall: float, *, failed: bool = False) -> CreativeScore:
    return CreativeScore(
        overall_score=0.0 if failed else overall,
        passed=not failed and overall >= 0.7,
        verdicts=[]
        if failed
        else [
            EvalVerdict(dimension="clarity", score=4, verdict="pass", rationale="ok")
        ],
        strengths=[],
        improvements=["evaluation_failed"] if failed else [],
    )


class FakeJudge:
    """Stands in for ``evaluate_all_concurrently``; records each call's inputs."""

    def __init__(self, failed_ad_ids: tuple[int, ...] = ()):
        self.calls: list[tuple[list[Any], list[Any]]] = []
        self.failed_ad_ids = failed_ad_ids

    def __call__(self, ad_copies, visual_concepts, campaign_context, config, **_kw):
        self.calls.append(
            ([a["original_id"] for a in ad_copies], list(visual_concepts))
        )
        ads = [
            AdCopyEvaluation(
                original_id=a["original_id"],
                headline=a["headline"],
                tone_style=a.get("tone_style", ""),
                score=_score(
                    0.5 + 0.1 * a["original_id"],
                    failed=a["original_id"] in self.failed_ad_ids,
                ),
            )
            for a in ad_copies
        ]
        visuals = [
            VisualConceptEvaluation(
                ad_copy_id=v["ad_copy_id"],
                concept_name=v["concept_name"],
                score=_score(0.65),
            )
            for v in visual_concepts
        ]
        return ads, visuals


def _state(**extra: Any) -> dict[str, Any]:
    return {
        **_CAMPAIGN,
        "ad_copy_critique": copy.deepcopy(_ADS),
        "final_visual_concepts": copy.deepcopy(_VISUALS),
        **extra,
    }


# --- evaluate_ad_copies_only --- #


def test_early_eval_judges_only_ad_copies_and_stores_partial(monkeypatch):
    judge = FakeJudge()
    monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", judge)
    ctx = FakeToolContext(_state())

    result = ev_agent.evaluate_ad_copies_only(ctx)

    assert result["status"] == "success"
    assert result["total_ad_copies"] == 4
    assert judge.calls == [([1, 2, 3, 4], [])]
    partial = ctx.state[ev_agent.AD_COPY_PARTIAL_KEY]
    assert partial["fingerprint"] == ev_agent.ad_copy_eval_fingerprint(ctx.state)
    assert [e["original_id"] for e in partial["evaluations"]] == [1, 2, 3, 4]
    assert "creative_evaluation_report" not in ctx.state


def test_early_eval_without_ad_copies_is_skipped(monkeypatch):
    judge = FakeJudge()
    monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", judge)
    ctx = FakeToolContext({**_CAMPAIGN, "ad_copy_critique": {"ad_copies": []}})

    result = ev_agent.evaluate_ad_copies_only(ctx)

    assert result["status"] == "skipped"
    assert judge.calls == []
    assert ev_agent.AD_COPY_PARTIAL_KEY not in ctx.state


# --- evaluate_all_creatives merging the partial --- #


def _full_report(monkeypatch, state: dict[str, Any]) -> tuple[dict, FakeJudge]:
    judge = FakeJudge()
    monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", judge)
    ctx = FakeToolContext(state)
    result = ev_agent.evaluate_all_creatives(ctx)
    assert result["status"] == "success"
    return ctx.state["creative_evaluation_report"], judge


def _with_partial(monkeypatch, **extra: Any) -> dict[str, Any]:
    monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", FakeJudge())
    ctx = FakeToolContext(_state(**extra))
    ev_agent.evaluate_ad_copies_only(ctx)
    return dict(ctx.state)


def test_finalize_reuses_partial_and_judges_only_visuals(monkeypatch):
    baseline, _ = _full_report(monkeypatch, _state())
    state = _with_partial(monkeypatch)

    report, judge = _full_report(monkeypatch, state)

    assert judge.calls == [([], _VISUALS["visual_concepts"])]
    # Identical report: same evaluations, same summary math, same warnings.
    assert report == baseline


def test_stale_partial_is_ignored_when_ad_copies_changed(monkeypatch):
    state = _with_partial(monkeypatch)
    state["ad_copy_critique"]["ad_copies"][0]["headline"] = "Rewritten"

    report, judge = _full_report(monkeypatch, state)

    assert judge.calls == [([1, 2, 3, 4], _VISUALS["visual_concepts"])]
    assert report["ad_copy_evaluations"][0]["headline"] == "Rewritten"


def test_stale_partial_is_ignored_when_brief_changed(monkeypatch):
    state = _with_partial(monkeypatch)
    state["creative_brief"] = {"proposition": "new"}

    _, judge = _full_report(monkeypatch, state)

    assert judge.calls[0][0] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "partial",
    [
        None,
        "garbage",
        {"fingerprint": "x"},
        {"evaluations": []},
        {"fingerprint": None, "evaluations": [{"bad": 1}]},
    ],
    ids=["none", "string", "no-evals", "no-fingerprint", "invalid-evals"],
)
def test_missing_or_malformed_partial_falls_back_to_full_eval(monkeypatch, partial):
    state = _state()
    if partial is not None:
        if isinstance(partial, dict) and partial.get("fingerprint") is None:
            partial = {
                **partial,
                "fingerprint": ev_agent.ad_copy_eval_fingerprint(state),
            }
        state[ev_agent.AD_COPY_PARTIAL_KEY] = partial

    _, judge = _full_report(monkeypatch, state)

    assert judge.calls == [([1, 2, 3, 4], _VISUALS["visual_concepts"])]


def test_partial_with_wrong_count_falls_back(monkeypatch):
    state = _with_partial(monkeypatch)
    state[ev_agent.AD_COPY_PARTIAL_KEY]["evaluations"].pop()

    _, judge = _full_report(monkeypatch, state)

    assert judge.calls[0][0] == [1, 2, 3, 4]


def test_failed_early_judge_calls_are_rejudged_in_finalize(monkeypatch):
    monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", FakeJudge((2,)))
    ctx = FakeToolContext(_state())
    ev_agent.evaluate_ad_copies_only(ctx)
    baseline, _ = _full_report(monkeypatch, _state())

    report, judge = _full_report(monkeypatch, dict(ctx.state))

    # Only the failed copy (#2) is re-judged, alongside the visuals; order kept.
    assert judge.calls == [([2], _VISUALS["visual_concepts"])]
    assert report == baseline


# --- finalize.evaluate_ad_copies_node --- #


def test_node_runs_off_loop_and_copies_partial_back(monkeypatch):
    from creative_agent import finalize as fin

    ctx = FakeToolContext(_state())
    seen: dict[str, Any] = {}

    def fake(holder):
        seen["thread"] = threading.current_thread()
        seen["same_state"] = holder.state is ctx.state
        holder.state[ev_agent.AD_COPY_PARTIAL_KEY] = {"fingerprint": "f"}
        return {"status": "success"}

    monkeypatch.setattr(fin.eval_agent, "evaluate_ad_copies_only", fake)
    asyncio.run(fin.evaluate_ad_copies_node(ctx))  # ty: ignore[invalid-argument-type]
    assert seen["thread"] is not threading.main_thread()
    assert seen["same_state"] is False
    assert ctx.state[ev_agent.AD_COPY_PARTIAL_KEY] == {"fingerprint": "f"}


@pytest.mark.parametrize(
    "fake",
    [
        lambda holder: (_ for _ in ()).throw(RuntimeError("judge down")),
        lambda holder: {"status": "skipped"},
    ],
    ids=["raises", "skipped"],
)
def test_node_failure_is_fail_soft(monkeypatch, fake):
    from creative_agent import finalize as fin

    ctx = FakeToolContext(_state())
    monkeypatch.setattr(fin.eval_agent, "evaluate_ad_copies_only", fake)
    asyncio.run(fin.evaluate_ad_copies_node(ctx))  # ty: ignore[invalid-argument-type]
    assert ev_agent.AD_COPY_PARTIAL_KEY not in ctx.state
    assert not [k for k in ctx.state if k.endswith(("__issues", "__retry_exhausted"))]
