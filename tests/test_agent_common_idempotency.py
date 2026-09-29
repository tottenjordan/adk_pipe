"""Tests for agent_common.idempotency.stable_row_id (pure, offline)."""

from agent_common.idempotency import stable_row_id


def test_deterministic():
    assert stable_row_id("sess-1", "trend") == stable_row_id("sess-1", "trend")


def test_default_length_is_8():
    # 8 chars keeps the creative_uuid / creative_row_uuid format CRF joins on.
    assert len(stable_row_id("sess-1")) == 8


def test_custom_length():
    assert len(stable_row_id("sess-1", length=16)) == 16


def test_different_parts_differ():
    assert stable_row_id("sess-1", "trend") != stable_row_id("sess-2", "trend")


def test_no_separator_collision():
    # json.dumps framing, not "|".join, so boundary shifts can't collide.
    assert stable_row_id("a|b", "c") != stable_row_id("a", "b|c")


def test_exported_from_package():
    import agent_common

    assert agent_common.stable_row_id is stable_row_id
    assert "stable_row_id" in agent_common.__all__
