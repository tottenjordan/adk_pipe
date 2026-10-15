"""poll_to_terminal must survive a transient slow/failed poll.

A late-run poll returns the full session state and can exceed the socket
timeout; a single such blip must not abort a run that is completing server-side.
"""

from __future__ import annotations

import urllib.error

import pytest

from experiments.creative_latency import run_trial


def test_poll_retries_transient_read_error(monkeypatch):
    """A one-off TimeoutError is retried; the next poll's terminal status wins."""
    calls = {"n": 0}

    def fake_get(url, token, timeout=run_trial.HTTP_TIMEOUT_S):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("The read operation timed out")
        if calls["n"] == 2:
            raise urllib.error.URLError("connection reset")
        return {
            "status": "done",
            "events": [{"id": "e1"}],
            "nextCursor": 1,
            "state": {"brand": "X"},
            "error": None,
        }

    monkeypatch.setattr(run_trial, "_get", fake_get)
    monkeypatch.setattr(run_trial.time, "sleep", lambda _s: None)

    events, state, status, error = run_trial.poll_to_terminal(
        "https://base", "user", "sid", "tok"
    )

    assert status == "done"
    assert error is None
    assert state == {"brand": "X"}
    assert events == [{"id": "e1"}]
    assert calls["n"] == 3  # two transient failures, then success


def test_poll_raises_timeout_when_never_terminal(monkeypatch):
    """If every poll fails/stalls past the deadline, it raises TimeoutError."""
    monkeypatch.setattr(run_trial, "POLL_TIMEOUT_S", 0.0)
    monkeypatch.setattr(run_trial, "_get", lambda *a, **k: {"status": "running"})
    monkeypatch.setattr(run_trial.time, "sleep", lambda _s: None)

    with pytest.raises(TimeoutError):
        run_trial.poll_to_terminal("https://base", "user", "sid", "tok")


def test_poll_fetches_full_state_after_an_incremental_terminal_poll(monkeypatch):
    """The api returns ``state`` only on a full replay (``since=0``), so a run whose
    terminal status arrives on an incremental poll must be re-read at ``since=0``
    for its final state, not keep the state of the first poll."""
    urls: list[str] = []
    replies = iter(
        [
            {
                "status": "running",
                "events": [{"id": "e1"}],
                "nextCursor": 1,
                "state": {"brand": "X"},
            },
            {"status": "done", "events": [{"id": "e2"}], "nextCursor": 2},
            {
                "status": "done",
                "events": [{"id": "e1"}, {"id": "e2"}],
                "nextCursor": 2,
                "state": {"brand": "X", "finalize_done": True},
            },
        ]
    )

    def fake_get(url, token, timeout=run_trial.HTTP_TIMEOUT_S):
        urls.append(url)
        return next(replies)

    monkeypatch.setattr(run_trial, "_get", fake_get)
    monkeypatch.setattr(run_trial.time, "sleep", lambda _s: None)

    events, state, status, _ = run_trial.poll_to_terminal(
        "https://base", "user", "sid", "tok"
    )

    assert status == "done"
    assert state == {"brand": "X", "finalize_done": True}
    assert events == [{"id": "e1"}, {"id": "e2"}]
    assert urls[-1].endswith("?since=0")


def test_poll_skips_the_extra_read_when_the_terminal_poll_was_a_full_replay(
    monkeypatch,
):
    """A run already terminal on the first (``since=0``) poll has its full state."""
    calls = {"n": 0}

    def fake_get(url, token, timeout=run_trial.HTTP_TIMEOUT_S):
        calls["n"] += 1
        return {
            "status": "done",
            "events": [],
            "nextCursor": 0,
            "state": {"finalize_done": True},
        }

    monkeypatch.setattr(run_trial, "_get", fake_get)
    monkeypatch.setattr(run_trial.time, "sleep", lambda _s: None)

    _, state, _, _ = run_trial.poll_to_terminal("https://base", "user", "sid", "tok")

    assert state == {"finalize_done": True}
    assert calls["n"] == 1


def test_poll_keeps_the_last_state_when_the_final_read_fails(monkeypatch):
    """A failed final full read keeps what the polls saw (data, not a crash)."""
    replies = iter(
        [
            {
                "status": "running",
                "events": [{"id": "e1"}],
                "nextCursor": 1,
                "state": {"brand": "X"},
            },
            {"status": "done", "events": [], "nextCursor": 1},
        ]
    )

    def fake_get(url, token, timeout=run_trial.HTTP_TIMEOUT_S):
        if url.endswith("?since=0") and not url.endswith("sid?since=0"):
            raise AssertionError("unexpected url")
        try:
            return next(replies)
        except StopIteration:
            raise urllib.error.URLError("gone") from None

    monkeypatch.setattr(run_trial, "_get", fake_get)
    monkeypatch.setattr(run_trial.time, "sleep", lambda _s: None)

    _, state, status, _ = run_trial.poll_to_terminal(
        "https://base", "user", "sid", "tok"
    )

    assert status == "done"
    assert state == {"brand": "X"}
