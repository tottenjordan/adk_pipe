"""Tests for trend_scout's debugging-observability callbacks.

These lock in the *predicate* behind `log_empty_turn_finish_reason` — the line
between a healthy turn (stay quiet) and a pathological empty/abnormal turn
(warn). A wrong predicate is silently costly: too loose spams the logs on every
normal tool call, too tight misses the exact MAX_TOKENS/MALFORMED empty turns
that cause the producer-empty landmine. `log_final_state_summary` is checked for
the skip-vs-empty signal it exists to provide.
"""

import logging
from types import SimpleNamespace

from google.genai import types
from google.adk.models.llm_response import LlmResponse

from trend_scout import callbacks


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


def test_normal_text_turn_is_silent(caplog):
    resp = _resp(
        parts=[types.Part(text="some analysis")],
        finish_reason=types.FinishReason.STOP,
    )
    with caplog.at_level(logging.WARNING):
        callbacks.log_empty_turn_finish_reason(_ctx(), resp)
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
        callbacks.log_empty_turn_finish_reason(_ctx(), resp)
    assert not caplog.records


def test_max_tokens_empty_turn_warns(caplog):
    """Thinking budget exhausted: MAX_TOKENS, no text, no tool call -> warn."""
    resp = _resp(parts=[], finish_reason=types.FinishReason.MAX_TOKENS)
    with caplog.at_level(logging.WARNING):
        callbacks.log_empty_turn_finish_reason(_ctx(), resp)
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
        callbacks.log_empty_turn_finish_reason(_ctx(), resp)
    assert len(caplog.records) == 1


def test_partial_streaming_chunk_is_ignored(caplog):
    resp = _resp(parts=[], finish_reason=types.FinishReason.MAX_TOKENS, partial=True)
    with caplog.at_level(logging.WARNING):
        callbacks.log_empty_turn_finish_reason(_ctx(), resp)
    assert not caplog.records


def test_final_state_summary_flags_missing_key(caplog):
    ctx = SimpleNamespace(
        invocation_id="inv-2",
        state={"raw_gtrends": ["a", "b"], "info_gtrends__retry_exhausted": True},
    )
    with caplog.at_level(logging.INFO):
        callbacks.log_final_state_summary(ctx)
    msg = caplog.records[-1].getMessage()
    assert "'raw_gtrends': 'present" in msg
    assert "'info_gtrends': 'MISSING'" in msg
    assert "'selected_gtrends': 'MISSING'" in msg
    assert "retry_exhausted=['info_gtrends__retry_exhausted']" in msg
