"""Tests for RetryUntilKeyAgent — the retry-on-empty producer wrapper.

These are fully offline: a fake inner agent (`FlakyProducer`, shared with the
graph-node tests via tests/_fakes.py) deterministically emits no `output_key`
for its first N runs, then a real value. Everything is
driven through a real `InMemoryRunner`, so the wrapper's state check exercises
the genuine ADK state-delta application path (runner appends each yielded event
and merges its `state_delta` into `session.state` before the wrapper resumes) —
not a mock of it. No model calls, no GCP credentials, no quota.

Coroutines are driven with `asyncio.run` (no pytest-asyncio in this project,
see tests/test_crf_worker_async.py).
"""

import asyncio
import logging

import pytest
from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from agent_common import RetryUntilKeyAgent
from tests._fakes import (
    FlakyFlagProducer,
    FlakyProducer,
    FlakySynthesizer,
    RawSearcher,
)


def _run(agent: BaseAgent):
    """Drive ``agent`` once through an InMemoryRunner; return the final session."""
    runner = InMemoryRunner(agent=agent, app_name="retry_test")

    async def _go():
        session = await runner.session_service.create_session(
            app_name="retry_test", user_id="u"
        )
        async for _ in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="go")]),
        ):
            pass
        return await runner.session_service.get_session(
            app_name="retry_test", user_id="u", session_id=session.id
        )

    return asyncio.run(_go())


def test_recovers_after_empty_attempts():
    """Producer fails twice, succeeds on the third — wrapper retries and recovers."""
    producer = FlakyProducer(name="producer", output_key="report", fail_first=2)
    wrapper = RetryUntilKeyAgent(
        name="retry_wrapper", sub_agents=[producer], output_key="report", max_attempts=3
    )

    session = _run(wrapper)

    assert producer.runs == 3
    assert session.state.get("report") == "REAL_REPORT"


def test_no_retry_when_first_attempt_succeeds():
    """A healthy producer runs exactly once — no wasted retries."""
    producer = FlakyProducer(name="producer", output_key="report", fail_first=0)
    wrapper = RetryUntilKeyAgent(
        name="retry_wrapper", sub_agents=[producer], output_key="report", max_attempts=3
    )

    session = _run(wrapper)

    assert producer.runs == 1
    assert session.state.get("report") == "REAL_REPORT"


def test_bounded_and_observable_when_never_populated(caplog):
    """Producer never populates — wrapper stops at max_attempts and logs loudly.

    The wrapper must NOT corrupt ``output_key`` with a placeholder (that would
    feed garbage to the downstream consumer). Instead it leaves the key unset,
    records an observable ``<key>__retry_exhausted`` marker, and logs an error —
    mitigation #3 (observable guardrail), not silent degradation.
    """
    producer = FlakyProducer(name="producer", output_key="report", fail_first=99)
    wrapper = RetryUntilKeyAgent(
        name="retry_wrapper", sub_agents=[producer], output_key="report", max_attempts=3
    )

    with caplog.at_level(logging.ERROR):
        session = _run(wrapper)

    assert producer.runs == 3
    assert session.state.get("report") is None
    assert session.state.get("report__retry_exhausted") is True
    assert any("report" in r.message for r in caplog.records)


def test_retries_sequential_pair_until_synthesizer_populates():
    """WS2: the wrapper watches a SequentialAgent[searcher, synthesizer] pair.

    RetryUntilKeyAgent runs only ``sub_agents[0]``, so the split producer wraps
    the [searcher, synthesizer] pair in a SequentialAgent and passes THAT as the
    sole sub_agent. Each retry must re-run the whole sequence (searcher AND
    synthesizer), not just the synthesizer, until the synthesizer writes the
    consumer-facing key.
    """
    searcher = RawSearcher(name="searcher", raw_key="report_raw")
    synth = FlakySynthesizer(
        name="synth", raw_key="report_raw", output_key="report", fail_first=2
    )
    pair = SequentialAgent(name="search_and_synthesize", sub_agents=[searcher, synth])
    wrapper = RetryUntilKeyAgent(
        name="retry_wrapper", sub_agents=[pair], output_key="report", max_attempts=3
    )

    session = _run(wrapper)

    # The whole pair re-ran three times: searcher runs each attempt too.
    assert searcher.runs == 3
    assert synth.runs == 3
    assert session.state.get("report") == "REAL_REPORT"
    assert session.state.get("report_raw") == "RAW_FINDINGS"


def test_sequential_pair_exhaustion_is_observable():
    """WS2: when the synthesizer never populates, the wrapped pair stops at
    max_attempts, leaves the key unset, and records the retry-exhausted marker
    (so WS3's degradation surfaces still fire on the split producer)."""
    searcher = RawSearcher(name="searcher", raw_key="report_raw")
    synth = FlakySynthesizer(
        name="synth", raw_key="report_raw", output_key="report", fail_first=99
    )
    pair = SequentialAgent(name="search_and_synthesize", sub_agents=[searcher, synth])
    wrapper = RetryUntilKeyAgent(
        name="retry_wrapper", sub_agents=[pair], output_key="report", max_attempts=3
    )

    session = _run(wrapper)

    assert searcher.runs == 3
    assert synth.runs == 3
    assert session.state.get("report") is None
    assert session.state.get("report__retry_exhausted") is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("REAL", True),
        ("  x ", True),
        ("", False),
        ("   ", False),
        (True, True),  # the image-generation flag
        (False, False),
        (["k.png"], True),  # non-empty artifact-keys list
        ([], False),
        (0, False),
        (None, False),
    ],
)
def test_is_populated_accepts_truthy_non_strings(value, expected):
    """A non-blank string OR any truthy non-string counts as populated.

    Research producers write a non-blank string; the image producer writes a
    bool ``_images_generated`` flag. Falsy values (blank string, ``[]``, ``0``,
    ``False``, ``None``) must count as unpopulated so the wrapper retries.
    """
    assert RetryUntilKeyAgent._is_populated(value) is expected


def test_recovers_when_producer_writes_bool_flag():
    """Bool-flag producer fails once, then sets the flag — wrapper recovers.

    Guards the visual_generator use case: generate_image signals success via a
    bool ``_images_generated`` flag, not a string, so the wrapper must treat a
    truthy flag as populated.
    """
    producer = FlakyFlagProducer(
        name="imggen", output_key="_images_generated", fail_first=1
    )
    wrapper = RetryUntilKeyAgent(
        name="imggen_resilient",
        sub_agents=[producer],
        output_key="_images_generated",
        max_attempts=3,
    )

    session = _run(wrapper)

    assert producer.runs == 2
    assert session.state.get("_images_generated") is True
    assert session.state.get("_images_generated__retry_exhausted") is None


def test_no_false_exhaustion_when_flag_set_first_try():
    """A healthy bool-flag producer runs exactly once — no false exhaustion."""
    producer = FlakyFlagProducer(
        name="imggen", output_key="_images_generated", fail_first=0
    )
    wrapper = RetryUntilKeyAgent(
        name="imggen_resilient",
        sub_agents=[producer],
        output_key="_images_generated",
        max_attempts=3,
    )

    session = _run(wrapper)

    assert producer.runs == 1
    assert session.state.get("_images_generated") is True
    assert session.state.get("_images_generated__retry_exhausted") is None
