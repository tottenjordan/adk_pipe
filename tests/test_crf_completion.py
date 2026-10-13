"""The worker's completion check (cloud_functions/creative_fanout/completion.py).

PROCESSED only on a truthy ``finalize_done``: a clean stream end is not enough
(spike 2026-10-13: a 499 failed creative_pipeline, the root ended the stream
cleanly); a stream timeout/cut is polled while the run still progresses
server-side. The ONE retry message needs the positive end-of-invocation signal
(the root's final text, no unanswered function call, stable for one poll):
quiet time alone never retries, it only gives up after ``idle_secs``. Fake
clock + sleep, so nothing waits.

Imports only the CRF package (the crf-deps CI job runs this file in a venv with
nothing but ``cloud_functions/creative_fanout/requirements.txt`` + pytest).
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_functions.creative_fanout import clients, worker
from cloud_functions.creative_fanout.completion import (
    MIN_RETRY_BUDGET_SECS,
    RETRY_MESSAGE,
    RunMonitor,
    _state_done,
    invocation_ended,
)
from tests.crf_fakes import FakeStatusStore

pytestmark = pytest.mark.usefixtures("gcp_project_env")

IDLE = 600.0
POLL = 30.0


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs


# --- session events ---------------------------------------------------------
def _ev(author, *parts, **extra):
    return {"author": author, "content": {"parts": list(parts)}, **extra}


def _text(text, **extra):
    return {"text": text, **extra}


def _call(name, call_id):
    return {"function_call": {"name": name, "id": call_id, "args": {}}}


def _resp(name, call_id):
    return {"function_response": {"name": name, "id": call_id, "response": {}}}


def _ended_invocation(n=1):
    """User message → root calls the pipeline → response → root's final text."""
    return [
        _ev("user", _text(f"go {n}")),
        _ev("root_agent", _call("creative_pipeline", f"c{n}")),
        _ev("synth", _text("sub-agent output")),
        _ev("root_agent", _resp("creative_pipeline", f"c{n}")),
        _ev("root_agent", _text("The pipeline failed with an error.")),
    ]


def _running_invocation(n_sub_events=1):
    """The root's pipeline call is still unanswered; sub-agents write text."""
    return [
        _ev("user", _text("go")),
        _ev("root_agent", _call("creative_pipeline", "c1")),
        *[_ev("synth", _text(f"step {i}")) for i in range(n_sub_events)],
    ]


def _session(events, done=None):
    state = {} if done is None else {"finalize_done": done}
    return {"id": "sess-1", "state": state, "events": events}


def _ended_session(agent, done_after=99):
    """Each streamed message ended unfinished; finalized once ``done_after``
    messages were sent."""
    events = []
    for n in range(1, len(agent.messages) + 1):
        events += _ended_invocation(n)
    return _session(events, done=True if len(agent.messages) >= done_after else None)


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
        self.read_kwargs: list[dict] = []
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
        self.read_kwargs.append(kwargs)
        return self.timeline(self)


def _monitor(agent, clock, deadline_in=1650.0, **overrides):
    kwargs = {
        "deadline": clock() + deadline_in,
        "stream_timeout_secs": 1500.0,
        "poll_interval_secs": POLL,
        "idle_secs": IDLE,
        "clock": clock,
        "sleep": clock.sleep,
    }
    kwargs.update(overrides)
    return RunMonitor(agent, "crf_worker_0", "sess-1", **kwargs)


def _run(agent, clock, **kwargs):
    outcome = asyncio.run(_monitor(agent, clock, **kwargs).run("go"))
    return (outcome.finalized, outcome.retried, outcome.reason)


# --- the end-of-invocation signal ------------------------------------------
def test_end_signal_root_final_text_after_answered_calls():
    assert invocation_ended(_session(_ended_invocation()))


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(_running_invocation(), id="pipeline_call_unanswered"),
        pytest.param(
            [*_running_invocation(), _ev("root_agent", _text("working on it"))],
            id="root_text_while_call_unanswered",
        ),
        pytest.param(
            [*_ended_invocation()[:-1], _ev("synth", _text("x"))],
            id="last_text_not_root",
        ),
        pytest.param(
            [*_ended_invocation()[:-1], _ev("root_agent", _text("t", thought=True))],
            id="thought_only",
        ),
        pytest.param(
            [*_ended_invocation()[:-1], _ev("root_agent", _text("  "))],
            id="blank_text",
        ),
        pytest.param(
            [*_ended_invocation()[:-1], _ev("root_agent", _text("x"), partial=True)],
            id="partial_chunk_skipped",
        ),
        pytest.param(
            [*_ended_invocation()[:-1], _ev("root_agent", _call("memorize", "m1"))],
            id="last_event_is_a_call",
        ),
        pytest.param(
            [
                *_ended_invocation()[:-1],
                _ev("root_agent", _text("x"), _call("memorize", "m1")),
            ],
            id="text_with_a_call",
        ),
        pytest.param(
            [*_ended_invocation(1), _ev("user", _text("retry"))],
            id="new_user_message",
        ),
        pytest.param([], id="no_events"),
    ],
)
def test_no_end_signal(events):
    assert not invocation_ended(_session(events))


def test_end_signal_ignores_trailing_events_without_content():
    events = [*_ended_invocation(), {"author": "root_agent", "actions": {"x": 1}}]
    assert invocation_ended(_session(events))


def test_end_signal_scoped_to_the_latest_invocation():
    """An unanswered call of an earlier invocation doesn't block the signal,
    one of the latest does."""
    earlier = [_ev("user", _text("go")), _ev("root_agent", _call("p", "old"))]
    assert invocation_ended(_session(earlier + _ended_invocation(2)))
    latest = _ended_invocation(2)
    latest.insert(1, _ev("root_agent", _call("p", "new")))
    assert not invocation_ended(_session(latest))


def test_end_signal_camel_case_and_root_name():
    events = [
        _ev("user", _text("go")),
        _ev("boss", {"functionCall": {"name": "p", "id": "1"}}),
        _ev("boss", {"functionResponse": {"name": "p", "id": "1"}}),
        _ev("boss", _text("done")),
    ]
    assert invocation_ended(_session(events), "boss")
    assert not invocation_ended(_session(events), "root_agent")


@pytest.mark.parametrize("value", [False, "", 0, None])
def test_falsy_finalize_done_is_not_done(value):
    assert not _state_done(_session([], done=value))
    assert _state_done(_session([], done=True))


# --- run(): done / retry / give up -------------------------------------------
def test_clean_stream_and_finalized_is_done_without_retry():
    clock = FakeClock()
    agent = FakeAgent(clock, ["clean"], lambda a: _session([], done=True))
    assert _run(agent, clock) == (True, False, "done")
    assert agent.messages == ["go"]
    assert clock.sleeps == []


@pytest.mark.parametrize("first_stream", ["clean", RuntimeError("boom")])
def test_ended_unfinished_sends_one_retry_after_one_confirming_poll(first_stream):
    clock = FakeClock()
    agent = FakeAgent(
        clock, [first_stream, "clean"], lambda a: _ended_session(a, done_after=2)
    )
    assert _run(agent, clock) == (True, True, "done_after_retry")
    assert agent.messages == ["go", RETRY_MESSAGE]
    # The signal is confirmed by ONE more read a poll later, not an idle window.
    assert clock.sleeps[0] == POLL
    assert agent.stream_kwargs[1]["message"] == RETRY_MESSAGE
    assert sum(clock.sleeps) == POLL


def test_still_not_done_after_retry_fails_without_a_second_retry():
    clock = FakeClock()
    agent = FakeAgent(clock, ["clean", "clean", "clean"], _ended_session)
    assert _run(agent, clock) == (False, True, "not_finalized_after_retry")
    assert agent.messages == ["go", RETRY_MESSAGE]


@pytest.mark.parametrize("value", [False, ""])
def test_falsy_finalize_done_run_is_not_finalized(value):
    clock = FakeClock()
    agent = FakeAgent(
        clock,
        ["clean", "clean"],
        lambda a: _session(_ended_invocation(len(a.messages)), done=value),
    )
    finalized, retried, _ = _run(agent, clock)
    assert (finalized, retried) == (False, True)


@pytest.mark.parametrize("first_stream", ["clean", TimeoutError("cut")])
def test_quiet_run_without_end_signal_is_never_retried(first_stream):
    """No new events for longer than any step, but the pipeline call is still
    unanswered: give up (stalled), never send the retry."""
    clock = FakeClock()
    start = clock()
    agent = FakeAgent(clock, [first_stream], lambda a: _session(_running_invocation()))
    assert _run(agent, clock) == (False, False, "stalled")
    assert agent.messages == ["go"]
    assert clock() - start >= IDLE


def test_end_signal_must_be_stable_across_a_poll():
    """The signal seen once, then new events (the run moved on): no retry
    until it holds on two reads with nothing new in between."""
    clock = FakeClock()
    reads = []

    def timeline(a):
        reads.append(clock())
        events = _ended_invocation(1)
        if len(reads) == 2:  # one more event between the 1st and 2nd read
            events = [*events, _ev("root_agent", _text("still talking"))]
        if len(reads) >= 3:
            events = [*events, _ev("root_agent", _text("still talking"))]
        return _session(events, done=True if len(a.messages) == 2 else None)

    agent = FakeAgent(clock, ["clean", "clean"], timeline)
    assert _run(agent, clock) == (True, True, "done_after_retry")
    # Retry sent only after the 3rd read (2nd and 3rd identical).
    assert sum(clock.sleeps[:2]) == 2 * POLL


def test_stream_cut_while_run_progresses_is_polled_until_done():
    clock = FakeClock()
    start = clock()

    def timeline(a):
        # The run keeps appending events server-side and finishes 600 s later.
        elapsed = clock() - start
        return _session(
            _running_invocation(int(elapsed // 30) + 1), done=elapsed >= 600 or None
        )

    agent = FakeAgent(clock, [TimeoutError("stream cut")], timeline)
    assert _run(agent, clock)[:2] == (True, False)
    assert agent.messages == ["go"]
    assert all(s == POLL for s in clock.sleeps)
    assert len(clock.sleeps) == 20


def test_retry_needs_min_budget_left():
    """Ended unfinished with < MIN_RETRY_BUDGET_SECS left: no retry."""
    clock = FakeClock()
    agent = FakeAgent(clock, ["clean", "clean"], lambda a: _ended_session(a, 2))
    # After the confirming poll 250 - 30 = 220 s are left (< 300, > 30).
    assert _run(agent, clock, deadline_in=250.0) == (False, False, "budget_exhausted")
    assert agent.messages == ["go"]
    assert MIN_RETRY_BUDGET_SECS == 300.0


def test_retry_sent_with_just_enough_budget():
    clock = FakeClock()
    agent = FakeAgent(clock, ["clean", "clean"], lambda a: _ended_session(a, 2))
    deadline_in = MIN_RETRY_BUDGET_SECS + POLL + 1
    assert _run(agent, clock, deadline_in=deadline_in) == (
        True,
        True,
        "done_after_retry",
    )


def test_budget_exhausted_while_still_running_fails_without_retry():
    clock = FakeClock()
    start = clock()

    def timeline(a):  # always progressing
        return _session(_running_invocation(int((clock() - start) // 10) + 1))

    agent = FakeAgent(clock, [TimeoutError()], timeline)
    assert _run(agent, clock, deadline_in=400.0) == (False, False, "budget_exhausted")
    assert agent.messages == ["go"]
    assert clock() - start <= 400.0


def test_unreadable_session_fails_without_retry():
    clock = FakeClock()

    def timeline(a):
        raise RuntimeError("get_session 500")

    agent = FakeAgent(clock, [RuntimeError("boom")], timeline)
    assert _run(agent, clock) == (False, False, "session_unreadable")


# --- bounded waits -------------------------------------------------------------
def test_stream_gets_explicit_timeout_clamped_to_budget():
    clock = FakeClock()
    agent = FakeAgent(clock, ["clean"], lambda a: _session([], done=True))
    _run(agent, clock, deadline_in=900.0)
    assert agent.stream_kwargs[0]["http_options"] == {"timeout": 900_000}


def test_stream_that_hangs_is_bounded_and_leads_into_the_check(monkeypatch):
    """A stream past its timeout ends in the check (not straight to FAILED)."""
    clock = FakeClock()

    class HangingAgent(FakeAgent):
        async def async_stream_query(self, **kwargs):
            self.messages.append(kwargs["message"])
            await asyncio.sleep(10)
            yield {}  # pragma: no cover

    agent = HangingAgent(clock, [], lambda a: _session([], done=True))
    monitor = _monitor(agent, clock)
    monitor.stream_timeout_secs = 0.01
    monkeypatch.setattr(monitor, "remaining", lambda: 0.01)
    assert asyncio.run(monitor.stream("go")) is False


def test_session_read_gets_the_read_timeout():
    clock = FakeClock()
    agent = FakeAgent(clock, [], lambda a: _session([]))
    asyncio.run(_monitor(agent, clock, read_timeout_secs=60.0).read_session())
    assert agent.read_kwargs[0]["http_options"] == {"timeout": 60_000}


def test_session_read_timeout_clamped_to_remaining_budget():
    clock = FakeClock()
    agent = FakeAgent(clock, [], lambda a: _session([]))
    asyncio.run(_monitor(agent, clock, deadline_in=12.0).read_session())
    assert agent.read_kwargs[0]["http_options"] == {"timeout": 12_000}


def test_session_read_that_hangs_is_bounded():
    clock = FakeClock()

    class HangingRead(FakeAgent):
        async def async_get_session(self, **kwargs):
            await asyncio.sleep(10)

    monitor = _monitor(HangingRead(clock, [], None), clock, read_timeout_secs=0.05)
    start = time.monotonic()
    assert asyncio.run(monitor.read_session()) is None
    assert time.monotonic() - start < 2


def test_no_read_once_the_budget_is_gone():
    clock = FakeClock()
    agent = FakeAgent(clock, [], lambda a: _session([], done=True))
    assert asyncio.run(_monitor(agent, clock, deadline_in=-1.0).read_session()) is None
    assert agent.reads == 0


def test_budget_rechecked_after_each_read():
    """A read that eats the rest of the budget ends the check right there as
    budget_exhausted (before the quiet-time give-up is even considered)."""
    clock = FakeClock()

    def timeline(a):
        clock.now += 25.0  # each read takes 25 s
        return _session(_running_invocation())

    agent = FakeAgent(clock, [], timeline)
    monitor = _monitor(agent, clock, deadline_in=80.0, idle_secs=50.0)
    result = asyncio.run(monitor.check())
    # read (25 s) + poll (30 s) + read (25 s) = the whole 80 s budget, and
    # 55 s without progress would also pass idle_secs: the budget wins.
    assert (result.reason, agent.reads) == ("budget_exhausted", 2)
    assert clock() == monitor.deadline


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
    fake_vertex.aio.aclose = AsyncMock()
    monkeypatch.setattr(clients, "new_vertex_client", lambda: fake_vertex)
    monkeypatch.setattr(worker, "_clock", clock)
    monkeypatch.setattr(worker, "_sleep", clock.sleep)
    store = FakeStatusStore(monkeypatch, _TREND)
    asyncio.run(
        worker.process_row(
            _TREND, "agent-123", MagicMock(), "ds", "tbl", deadline=clock() + 1650.0
        )
    )
    return store.statuses


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
    agent = FakeAgent(clock, streams, lambda a: _ended_session(a, done_after))
    assert _wire(monkeypatch, agent, clock) == [expected]
    # Deleted once, with the creating user_id, only after the check read it.
    # (The id names the re-read row: crf_worker_<hash(uuid, target_trend)>.)
    (user_id,) = agent.created
    assert user_id.startswith("crf_worker_")
    assert agent.deleted == [(user_id, "sess-1")]
    assert agent.delete_after_reads == agent.reads and agent.reads >= 1


def test_worker_wires_the_configured_read_timeout(monkeypatch):
    monkeypatch.setenv("CRF_SESSION_READ_TIMEOUT_SECS", "45")
    clock = FakeClock()
    agent = FakeAgent(clock, ["clean"], lambda a: _session([], done=True))
    assert _wire(monkeypatch, agent, clock) == ["PROCESSED"]
    assert agent.read_kwargs[0]["http_options"] == {"timeout": 45_000}


def test_worker_logs_finalized_and_retried(monkeypatch, caplog):
    import logging

    clock = FakeClock()
    agent = FakeAgent(clock, ["clean", "clean"], lambda a: _ended_session(a, 2))
    with caplog.at_level(logging.INFO):
        _wire(monkeypatch, agent, clock)
    (record,) = [
        r for r in caplog.records if r.getMessage().startswith("AGENT_RUN_DURATION")
    ]
    fields = record.json_fields
    assert fields["status"] == "PROCESSED"
    assert fields["finalized"] is True
    assert fields["retried"] is True
