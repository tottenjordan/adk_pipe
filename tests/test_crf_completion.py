"""The worker's completion check (cloud_functions/creative_fanout/completion.py).

PROCESSED only on ``finalize_done``: a clean stream end is not enough (spike
2026-10-13: a 499 failed creative_pipeline, the root ended the stream cleanly);
a stream timeout/cut is polled while the run still progresses server-side; an
idle unfinished run gets exactly ONE retry message. Fake clock + sleep, so
nothing waits.

Imports only the CRF package (the crf-deps CI job runs this file in a venv with
nothing but ``cloud_functions/creative_fanout/requirements.txt`` + pytest).
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from cloud_functions.creative_fanout import main
from cloud_functions.creative_fanout.completion import (
    RETRY_MESSAGE,
    RunMonitor,
)

pytestmark = pytest.mark.usefixtures("gcp_project_env")


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs


class FakeAgent:
    """Scripted engine: per stream call a behaviour, per session read a state.

    ``streams``: list of "clean" | Exception instances, one per stream call.
    ``timeline``: callable(agent) -> session dict, evaluated on each read.
    """

    def __init__(self, clock, streams, timeline):
        self.clock = clock
        self.streams = list(streams)
        self.timeline = timeline
        self.messages: list[str] = []
        self.stream_kwargs: list[dict] = []
        self.reads = 0
        self.deleted: list[tuple[str, str]] = []
        self.created: list[str] = []
        self.delete_after_reads: int | None = None

    async def async_create_session(self, *, user_id, state=None):
        self.created.append(user_id)
        return {"id": "sess-1"}

    async def async_delete_session(self, *, user_id, session_id):
        self.delete_after_reads = self.reads
        self.deleted.append((user_id, session_id))

    async def async_stream_query(self, **kwargs):
        self.stream_kwargs.append(kwargs)
        self.messages.append(kwargs["message"])
        behaviour = self.streams.pop(0)
        yield {"author": "root_agent", "content": {"parts": [{"text": "hi"}]}}
        if isinstance(behaviour, BaseException):
            raise behaviour

    async def async_get_session(self, *, user_id, session_id, **kwargs):
        self.reads += 1
        return self.timeline(self)


def _session(n_events, done=False):
    state = {"finalize_done": True} if done else {}
    return {"id": "sess-1", "state": state, "events": [{}] * n_events}


def _monitor(agent, clock, deadline_in=1650.0):
    return RunMonitor(
        agent,
        "crf_worker_0",
        "sess-1",
        deadline=clock() + deadline_in,
        stream_timeout_secs=1500.0,
        poll_interval_secs=30.0,
        idle_secs=180.0,
        clock=clock,
        sleep=clock.sleep,
    )


def test_clean_stream_and_finalized_is_done_without_retry():
    clock = FakeClock()
    agent = FakeAgent(clock, ["clean"], lambda a: _session(10, done=True))
    outcome = asyncio.run(_monitor(agent, clock).run("go"))
    assert (outcome.finalized, outcome.retried, outcome.reason) == (
        True,
        False,
        "done",
    )
    assert agent.messages == ["go"]
    assert clock.sleeps == []


def test_clean_stream_idle_not_done_sends_one_retry_then_done():
    clock = FakeClock()

    def timeline(a):
        # Finished only once the retry message was streamed.
        return _session(12, done=len(a.messages) == 2)

    agent = FakeAgent(clock, ["clean", "clean"], timeline)
    outcome = asyncio.run(_monitor(agent, clock).run("go"))
    assert outcome.finalized and outcome.retried
    assert agent.messages == ["go", RETRY_MESSAGE]


def test_still_not_done_after_retry_fails_without_a_second_retry():
    clock = FakeClock()
    agent = FakeAgent(clock, ["clean", "clean", "clean"], lambda a: _session(5))
    outcome = asyncio.run(_monitor(agent, clock).run("go"))
    assert not outcome.finalized
    assert outcome.retried
    assert outcome.reason == "not_finalized_after_retry"
    assert agent.messages == ["go", RETRY_MESSAGE]


def test_stream_cut_while_run_progresses_is_polled_until_done():
    clock = FakeClock()
    start = clock()

    def timeline(a):
        # The run keeps appending events server-side and finishes 600 s later.
        elapsed = clock() - start
        return _session(int(elapsed // 30), done=elapsed >= 600)

    agent = FakeAgent(clock, [TimeoutError("stream cut")], timeline)
    outcome = asyncio.run(_monitor(agent, clock).run("go"))
    assert (outcome.finalized, outcome.retried) == (True, False)
    assert agent.messages == ["go"]
    assert all(s == 30.0 for s in clock.sleeps)
    assert len(clock.sleeps) == 20


def test_stream_error_then_idle_needs_the_full_idle_window_before_retry():
    clock = FakeClock()
    agent = FakeAgent(
        clock,
        [RuntimeError("boom"), "clean"],
        lambda a: _session(3, done=len(a.messages) == 2),
    )
    outcome = asyncio.run(_monitor(agent, clock).run("go"))
    assert outcome.finalized and outcome.retried
    # 180 s quiet after the error before the retry (6 polls of 30 s).
    assert sum(clock.sleeps[:6]) == 180.0


def test_budget_exhausted_while_still_running_fails_without_retry():
    clock = FakeClock()
    start = clock()

    def timeline(a):
        return _session(int((clock() - start) // 10))  # always progressing

    agent = FakeAgent(clock, [TimeoutError()], timeline)
    outcome = asyncio.run(_monitor(agent, clock, deadline_in=400.0).run("go"))
    assert (outcome.finalized, outcome.retried, outcome.reason) == (
        False,
        False,
        "budget_exhausted",
    )
    assert agent.messages == ["go"]
    assert clock() - start <= 400.0


def test_unreadable_session_fails_without_retry():
    clock = FakeClock()

    def timeline(a):
        raise RuntimeError("get_session 500")

    agent = FakeAgent(clock, [RuntimeError("boom")], timeline)
    outcome = asyncio.run(_monitor(agent, clock).run("go"))
    assert (outcome.finalized, outcome.retried, outcome.reason) == (
        False,
        False,
        "session_unreadable",
    )


def test_stream_gets_explicit_timeout_clamped_to_budget():
    clock = FakeClock()
    agent = FakeAgent(clock, ["clean"], lambda a: _session(1, done=True))
    asyncio.run(_monitor(agent, clock, deadline_in=900.0).run("go"))
    assert agent.stream_kwargs[0]["http_options"] == {"timeout": 900_000}


def test_stream_that_hangs_is_bounded_and_leads_into_the_check(monkeypatch):
    """A stream past its timeout ends in the check (not straight to FAILED)."""
    clock = FakeClock()

    class HangingAgent(FakeAgent):
        async def async_stream_query(self, **kwargs):
            self.messages.append(kwargs["message"])
            await asyncio.sleep(10)
            yield {}  # pragma: no cover

    agent = HangingAgent(clock, [], lambda a: _session(1, done=True))
    monitor = _monitor(agent, clock)
    monitor.stream_timeout_secs = 0.01
    monkeypatch.setattr(monitor, "remaining", lambda: 0.01)
    assert asyncio.run(monitor.stream("go")) is False


# --- wired through the worker ---------------------------------------------------
_TREND = {
    "entry_timestamp": "2026-10-13T00:00:00",
    "index": 0,
    "brand": "BrandX",
    "target_product": "prod",
    "key_selling_point": "ksp",
    "target_audience": "aud",
    "target_search_trend": "trend",
}


def _wire(monkeypatch, agent, clock):
    fake_vertex = MagicMock()
    fake_vertex.runtimes.get.return_value = agent
    monkeypatch.setattr(main, "_new_vertex_client", lambda: fake_vertex)
    monkeypatch.setattr(main, "_clock", clock)
    monkeypatch.setattr(main, "_sleep", clock.sleep)
    monkeypatch.setattr(main, "acquire_processing_lock", lambda *a, **k: True)
    update = MagicMock()
    monkeypatch.setattr(main, "update_rows_status", update)
    asyncio.run(
        main._execute_agent_and_update_status(
            _TREND, "agent-123", MagicMock(), "ds", "tbl"
        )
    )
    return [c.kwargs["status"] for c in update.call_args_list]


@pytest.mark.parametrize(
    ("streams", "done_after", "expected"),
    [
        (["clean"], 1, "PROCESSED"),  # finalized on the first stream
        (["clean", "clean"], 2, "PROCESSED"),  # finalized after the retry
        (["clean", "clean"], 99, "FAILED"),  # never finalized
        ([TimeoutError(), "clean"], 99, "FAILED"),
    ],
)
def test_worker_marks_processed_only_when_finalized(
    monkeypatch, streams, done_after, expected
):
    clock = FakeClock()
    agent = FakeAgent(
        clock, streams, lambda a: _session(4, done=len(a.messages) >= done_after)
    )
    assert _wire(monkeypatch, agent, clock) == [expected]
    # Deleted once, with the creating user_id, only after the check read it.
    assert agent.deleted == [("crf_worker_0", "sess-1")]
    assert agent.created == ["crf_worker_0"]
    assert agent.delete_after_reads == agent.reads and agent.reads >= 1


def test_worker_logs_finalized_and_retried(monkeypatch, caplog):
    import logging

    clock = FakeClock()
    agent = FakeAgent(
        clock, ["clean", "clean"], lambda a: _session(4, done=len(a.messages) == 2)
    )
    with caplog.at_level(logging.INFO):
        _wire(monkeypatch, agent, clock)
    (record,) = [
        r for r in caplog.records if r.getMessage().startswith("AGENT_RUN_DURATION")
    ]
    fields = record.json_fields
    assert fields["status"] == "PROCESSED"
    assert fields["finalized"] is True
    assert fields["retried"] is True


def test_completion_budget_defaults_and_env(monkeypatch):
    from cloud_functions.creative_fanout.config import config

    assert config.WORKER_TIMEOUT_SECS == 1800.0
    assert config.STREAM_TIMEOUT_SECS == 1500.0
    assert config.POLL_INTERVAL_SECS == 30.0
    assert config.IDLE_SECS == 180.0
    assert config.BUDGET_SAFETY_SECS == 150.0
    monkeypatch.setenv("CRF_IDLE_SECS", "5")
    assert config.IDLE_SECS == 30.0  # clamped
    monkeypatch.setenv("CRF_STREAM_TIMEOUT_SECS", "nope")
    assert config.STREAM_TIMEOUT_SECS == 1500.0
