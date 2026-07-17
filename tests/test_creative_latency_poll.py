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
