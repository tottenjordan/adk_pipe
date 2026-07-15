"""Tests for the shared observability callbacks in `agent_common`.

These lock in the agent-agnostic building blocks that WS3 extracts from
`trend_scout/callbacks.py` so every agent package can share them:

- `log_empty_turn_finish_reason` — the predicate between a healthy model turn
  (stay quiet) and a pathological empty/abnormal one (warn). Too loose spams the
  logs on every normal tool call; too tight misses the exact
  MAX_TOKENS/MALFORMED empty turns behind the producer-empty landmine.
- `make_final_state_summary(label, keys)` — a factory so each agent gets an
  end-of-run state summary keyed by its own load-bearing keys, without the
  hard-coded tuple the trend_scout version had.
- `collect_degradation_warnings(state)` — the single source of truth for turning
  `*__retry_exhausted` markers into human-readable degradation notes.

The state-summary and degradation tests use a REAL ADK `State` (not a plain
dict): `State` supports `.get()`/`__contains__` but NOT iteration, so
`for k in state` raises `KeyError: 0`. A dict here would be a false oracle — it
was, and it crashed live on 2026-07-14 (commit 9ec1c92).
"""

import logging
from types import SimpleNamespace

from google.genai import types
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions.state import State

from agent_common import observability


def _ctx():
    return SimpleNamespace(agent_name="understand_trends_agent", invocation_id="inv-1")


def _resp(*, parts=None, finish_reason=None, partial=None):
    content = types.Content(role="model", parts=parts) if parts is not None else None
    return LlmResponse(
        content=content,
        finish_reason=finish_reason,
        partial=partial,
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100,
            candidates_token_count=0,
            thoughts_token_count=200,
        ),
    )


# --- log_empty_turn_finish_reason -------------------------------------------


def test_normal_text_turn_is_silent(caplog):
    resp = _resp(
        parts=[types.Part(text="some analysis")],
        finish_reason=types.FinishReason.STOP,
    )
    with caplog.at_level(logging.WARNING):
        observability.log_empty_turn_finish_reason(_ctx(), resp)
    assert not caplog.records


def test_tool_call_turn_is_silent(caplog):
    """A google_search tool call (STOP + function_call, no text) is normal."""
    resp = _resp(
        parts=[
            types.Part(function_call=types.FunctionCall(name="google_search", args={}))
        ],
        finish_reason=types.FinishReason.STOP,
    )
    with caplog.at_level(logging.WARNING):
        observability.log_empty_turn_finish_reason(_ctx(), resp)
    assert not caplog.records


def test_max_tokens_empty_turn_warns(caplog):
    """Thinking budget exhausted: MAX_TOKENS, no text, no tool call -> warn."""
    resp = _resp(parts=[], finish_reason=types.FinishReason.MAX_TOKENS)
    with caplog.at_level(logging.WARNING):
        observability.log_empty_turn_finish_reason(_ctx(), resp)
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "MAX_TOKENS" in msg
    assert "understand_trends_agent" in msg
    assert "thoughts_tokens=200" in msg


def test_stop_but_empty_turn_warns(caplog):
    """A 'successful' STOP that produced neither text nor a tool call is the
    exact failure that leaves output_key unset -> warn."""
    resp = _resp(parts=[], finish_reason=types.FinishReason.STOP)
    with caplog.at_level(logging.WARNING):
        observability.log_empty_turn_finish_reason(_ctx(), resp)
    assert len(caplog.records) == 1


def test_partial_streaming_chunk_is_ignored(caplog):
    resp = _resp(parts=[], finish_reason=types.FinishReason.MAX_TOKENS, partial=True)
    with caplog.at_level(logging.WARNING):
        observability.log_empty_turn_finish_reason(_ctx(), resp)
    assert not caplog.records


# --- make_final_state_summary -----------------------------------------------


def test_final_state_summary_flags_missing_key(caplog):
    summary_cb = observability.make_final_state_summary(
        "trend_scout", ("raw_gtrends", "info_gtrends", "selected_gtrends")
    )
    ctx = SimpleNamespace(
        invocation_id="inv-2",
        state=State(
            value={"raw_gtrends": ["a", "b"], "info_gtrends__retry_exhausted": True},
            delta={},
        ),
    )
    with caplog.at_level(logging.INFO):
        summary_cb(ctx)
    msg = caplog.records[-1].getMessage()
    assert "trend_scout final state" in msg
    assert "'raw_gtrends': 'present" in msg
    assert "'info_gtrends': 'MISSING'" in msg
    assert "'selected_gtrends': 'MISSING'" in msg
    assert "retry_exhausted=['info_gtrends__retry_exhausted']" in msg


def test_final_state_summary_uses_its_label_and_keys(caplog):
    """The factory is parameterized: a different label + key tuple is honored."""
    summary_cb = observability.make_final_state_summary(
        "creative_agent", ("combined_final_cited_report", "creative_evaluation_report")
    )
    ctx = SimpleNamespace(
        invocation_id="inv-3",
        state=State(value={"combined_final_cited_report": "x" * 50}, delta={}),
    )
    with caplog.at_level(logging.INFO):
        summary_cb(ctx)
    msg = caplog.records[-1].getMessage()
    assert "creative_agent final state" in msg
    assert "'combined_final_cited_report': 'present" in msg
    assert "'creative_evaluation_report': 'MISSING'" in msg
    # No markers -> no retry_exhausted suffix.
    assert "retry_exhausted" not in msg


# --- collect_degradation_warnings -------------------------------------------


def test_collect_degradation_warnings_clean_state():
    state = State(value={"raw_gtrends": ["a"], "info_gtrends": "ok"}, delta={})
    assert observability.collect_degradation_warnings(state) == []


def test_collect_degradation_warnings_one_per_marker():
    state = State(
        value={
            "gs_web_search_insights__retry_exhausted": True,
            "campaign_web_search_insights__retry_exhausted": True,
            "some_other_key": "value",
        },
        delta={},
    )
    warnings = observability.collect_degradation_warnings(state)
    assert len(warnings) == 2
    # Sorted, and each names its research step without the marker suffix.
    assert warnings[0].startswith("Step 'campaign_web_search_insights'")
    assert warnings[1].startswith("Step 'gs_web_search_insights'")
    assert "__retry_exhausted" not in warnings[0]


def test_collect_degradation_warnings_ignores_falsy_markers():
    state = State(value={"gs_web_search_insights__retry_exhausted": False}, delta={})
    assert observability.collect_degradation_warnings(state) == []


def test_collect_degradation_warnings_accepts_plain_dict():
    """Callers may pass a plain dict (e.g. a state snapshot) — no iteration crash."""
    warnings = observability.collect_degradation_warnings(
        {"refined_web_search_insights__retry_exhausted": True}
    )
    assert len(warnings) == 1
    assert warnings[0].startswith("Step 'refined_web_search_insights'")
