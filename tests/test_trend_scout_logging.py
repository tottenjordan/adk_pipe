"""Wiring tests for trend_scout's debugging-observability callbacks.

The callback *behaviour* (empty-turn predicate, final-state summary format) is
covered once in `test_observability.py`; `trend_scout/callbacks.py` only
re-exports the shared functions. These lock in that wiring: trend_scout uses the
shared empty-turn logger, and its final-state summary carries the trend_scout
label + load-bearing keys and is attached to the root agent.
"""

import logging
from types import SimpleNamespace

from google.adk.sessions.state import State

from agent_common import observability
from trend_scout import callbacks


def test_empty_turn_callback_is_the_shared_observability_function():
    assert (
        callbacks.log_empty_turn_finish_reason
        is observability.log_empty_turn_finish_reason
    )


def test_final_state_summary_uses_trend_scout_label_and_keys(caplog):
    ctx = SimpleNamespace(
        invocation_id="inv-2",
        state=State(
            value={"raw_gtrends": ["a", "b"], "info_gtrends__retry_exhausted": True},
            delta={},
        ),
    )
    with caplog.at_level(logging.INFO):
        callbacks.log_final_state_summary(ctx)
    msg = caplog.records[-1].getMessage()
    assert "trend_scout final state" in msg
    assert "'raw_gtrends': 'present" in msg
    assert "'info_gtrends': 'MISSING'" in msg
    assert "'selected_gtrends': 'MISSING'" in msg
    assert "retry_exhausted=['info_gtrends__retry_exhausted']" in msg


def test_root_agent_wires_the_observability_callbacks():
    from trend_scout.agent import root_agent

    assert root_agent.after_model_callback is callbacks.log_empty_turn_finish_reason
    assert root_agent.after_agent_callback is callbacks.log_final_state_summary
