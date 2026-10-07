"""interactive_creative checkpoint-2 user revision: the deterministic state prep
(``prepare_copy_revision``), the root's after-tool callback that clears its
inputs after ``ad_copy_user_reviser`` runs, and the reviser node's fail-soft
``on_error``."""

from types import SimpleNamespace
from typing import Any

from creative_agent.copy_gate import restore_unflagged
from interactive_creative.callbacks import (
    clear_copy_revision_inputs,
    user_copy_revision_failed,
)
from interactive_creative.review_tools import (
    USER_COPY_REVISIONS_KEY,
    prepare_copy_revision,
)
from tests._fakes import FakeToolContext


def _copy(original_id: int, headline: str) -> dict[str, Any]:
    return {"original_id": original_id, "headline": headline, "body_text": "b"}


COPIES = {"ad_copies": [_copy(1, "Beep beep"), _copy(2, "Zoom")]}


def test_prepare_flags_every_copy_with_the_feedback():
    ctx = FakeToolContext({"ad_copy_critique": COPIES})
    result = prepare_copy_revision("  Make them funnier  ", ctx)  # type: ignore[arg-type]

    assert result["status"] == "ready"
    assert result["copies_to_revise"] == 2
    state = ctx.state
    assert state["ad_copy_feedback"] == "Make them funnier"
    assert state["ad_copy_flagged_ids"] == ["1", "2"]
    issues = state["ad_copy_issues"]
    assert "Beep beep" in issues and "Zoom" in issues
    assert issues.count("Make them funnier") == 2
    assert state["ad_copy_critique__before_revision"] == COPIES
    assert state[USER_COPY_REVISIONS_KEY] == 1


def test_prepared_flags_let_the_reviser_change_every_copy():
    """restore_unflagged (the reviser's safety net) keeps every revised copy,
    because all of them are flagged."""
    ctx = FakeToolContext({"ad_copy_critique": COPIES})
    prepare_copy_revision("funnier", ctx)  # type: ignore[arg-type]
    revised = {"ad_copies": [_copy(1, "Beep BEEP"), _copy(2, "ZOOM")]}
    restored, notes = restore_unflagged(
        ctx.state["ad_copy_critique__before_revision"],
        revised,
        ctx.state["ad_copy_flagged_ids"],
    )
    assert restored == revised and notes == []


def test_prepare_skips_without_feedback_copies_or_after_one_revision():
    cases = [
        ({"ad_copy_critique": COPIES}, "   "),
        ({}, "funnier"),
        ({"ad_copy_critique": COPIES, USER_COPY_REVISIONS_KEY: 1}, "funnier"),
    ]
    for state, feedback in cases:
        ctx = FakeToolContext(state)
        result = prepare_copy_revision(feedback, ctx)  # type: ignore[arg-type]
        assert result["status"] == "skipped", state
        assert result["reason"]
        assert "ad_copy_flagged_ids" not in ctx.state
        assert "ad_copy_feedback" not in ctx.state


_PREPARED = {
    "ad_copy_issues": "- x",
    "ad_copy_flagged_ids": ["1"],
    "ad_copy_critique__before_revision": COPIES,
    "ad_copy_feedback": "funnier",
}


def test_clear_inputs_only_after_the_ad_copy_reviser():
    ctx = FakeToolContext(_PREPARED)
    other = SimpleNamespace(name="visual_concept_reviser")
    assert clear_copy_revision_inputs(other, {}, ctx, {}) is None  # type: ignore[arg-type]
    assert ctx.state["ad_copy_flagged_ids"] == ["1"]

    reviser = SimpleNamespace(name="ad_copy_user_reviser")
    assert clear_copy_revision_inputs(reviser, {}, ctx, {}) is None  # type: ignore[arg-type]
    assert ctx.state["ad_copy_issues"] == ""
    assert ctx.state["ad_copy_flagged_ids"] is None
    assert ctx.state["ad_copy_critique__before_revision"] is None
    # Kept for the visual steps.
    assert ctx.state["ad_copy_feedback"] == "funnier"


def test_failed_reviser_keeps_the_pre_revision_copies_and_clears_inputs():
    delta = user_copy_revision_failed(
        {**_PREPARED, "ad_copy_critique": COPIES}, RuntimeError("boom")
    )
    assert delta == {
        "ad_copy_issues": "",
        "ad_copy_flagged_ids": None,
        "ad_copy_critique__before_revision": None,
        "ad_copy_critique": COPIES,
        "ad_copy_user_revision_failed": True,
    }
