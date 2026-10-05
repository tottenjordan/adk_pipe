"""Root orchestrators re-ask an empty model turn instead of ending the run.

Nightly eval 2026-10-05 (run 37355680507, anr_skincare_creative): after
``visual_production_pipeline`` returned, the creative_agent root's
gemini-3.1-pro-preview turn came back ``STOP`` with no text and no function call
(``empty/abnormal model turn in root_agent``), so ADK ended the invocation before
evaluation and persistence. The runserver auto-continue (#212) only covers the
async ``/runs`` path, not ``adk eval`` or Agent Engine, so the re-ask lives in
the model (``TimeoutRetryingGemini.empty_turn_retries``), opt-in for the roots.
"""

import asyncio

import pytest
from google.adk.models import FallbackModel, Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from agent_common.models import (
    ROOT_EMPTY_TURN_RETRIES,
    build_gemini,
    build_gemini_with_fallback,
    is_empty_turn,
)


def _resp(*parts: types.Part, finish=types.FinishReason.STOP, **kw) -> LlmResponse:
    content = types.Content(role="model", parts=list(parts)) if parts else None
    return LlmResponse(content=content, finish_reason=finish, **kw)


EMPTY = _resp()
CALL = _resp(types.Part(function_call=types.FunctionCall(name="creative_eval_agent")))
TEXT = _resp(types.Part(text="done: gs://bucket/folder"))


# --- is_empty_turn ------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (EMPTY, True),
        (_resp(finish=None), True),
        (_resp(types.Part(text="")), True),
        (_resp(types.Part(text="planning...", thought=True)), True),
        (CALL, False),
        (TEXT, False),
        # Abnormal finishes / errors are not re-asked: a retry would not help.
        (_resp(finish=types.FinishReason.MAX_TOKENS), False),
        (_resp(finish=types.FinishReason.MALFORMED_FUNCTION_CALL), False),
        (_resp(finish=types.FinishReason.SAFETY), False),
        (_resp(error_code="500"), False),
        (_resp(partial=True), False),
    ],
)
def test_is_empty_turn(response, expected):
    assert is_empty_turn(response) is expected


# --- generate_content_async ---------------------------------------------------


def _patch_parent(monkeypatch, script):
    """Gemini.generate_content_async yields the next scripted response per call."""
    calls = []

    async def fake(self, llm_request, stream=False):
        calls.append(self.model)
        yield script[self.model].pop(0)

    monkeypatch.setattr(Gemini, "generate_content_async", fake)
    return calls


def _drain(model, stream=False):
    async def run():
        return [r async for r in model.generate_content_async(LlmRequest(), stream)]

    return asyncio.run(run())


def test_empty_turn_is_reasked_then_returns_the_real_turn(monkeypatch):
    calls = _patch_parent(monkeypatch, {"m": [EMPTY, CALL]})
    out = _drain(build_gemini("m", empty_turn_retries=2))
    assert out == [CALL]
    assert calls == ["m", "m"]


def test_empty_turn_retries_are_bounded_and_last_response_passes_through(monkeypatch):
    calls = _patch_parent(monkeypatch, {"m": [EMPTY, EMPTY, EMPTY, CALL]})
    out = _drain(build_gemini("m", empty_turn_retries=2))
    assert out == [EMPTY]  # exhausted: behaves exactly like no retry
    assert len(calls) == 3


def test_non_empty_turn_is_not_retried(monkeypatch):
    calls = _patch_parent(monkeypatch, {"m": [TEXT, CALL]})
    assert _drain(build_gemini("m", empty_turn_retries=2)) == [TEXT]
    assert calls == ["m"]


def test_default_model_never_reasks(monkeypatch):
    calls = _patch_parent(monkeypatch, {"m": [EMPTY, CALL]})
    assert _drain(build_gemini("m")) == [EMPTY]
    assert calls == ["m"]


def test_streaming_is_never_buffered_or_reasked(monkeypatch):
    calls = _patch_parent(monkeypatch, {"m": [EMPTY, CALL]})
    assert _drain(build_gemini("m", empty_turn_retries=2), stream=True) == [EMPTY]
    assert calls == ["m"]


def test_fallback_delegates_both_reask(monkeypatch):
    fb = build_gemini_with_fallback("pro", "flash", empty_turn_retries=2)
    assert isinstance(fb, FallbackModel)
    assert [m.empty_turn_retries for m in fb.models] == [2, 2]
    calls = _patch_parent(monkeypatch, {"pro": [EMPTY, CALL], "flash": []})
    assert _drain(fb) == [CALL]
    assert calls == ["pro", "pro"]


def test_fallback_kill_switch_keeps_empty_turn_retries():
    m = build_gemini_with_fallback("pro", "", empty_turn_retries=2)
    assert m.empty_turn_retries == 2


# --- wiring: only the root orchestrators opt in -------------------------------


def _delegates(model):
    return model.models if isinstance(model, FallbackModel) else [model]


def test_root_orchestrators_reask_empty_turns():
    from creative_agent.agent import root_agent as creative_root
    from interactive_creative.agent import root_agent as interactive_root
    from trend_scout.agent import root_agent as scout_root

    assert ROOT_EMPTY_TURN_RETRIES >= 1
    for root in (creative_root, interactive_root, scout_root):
        for m in _delegates(root.model):
            assert m.empty_turn_retries == ROOT_EMPTY_TURN_RETRIES, root.name


def test_sub_agents_do_not_reask_empty_turns():
    from creative_agent.agent import ad_copy_critic, visual_generator

    for agent in (ad_copy_critic, visual_generator):
        for m in _delegates(agent.model):
            assert m.empty_turn_retries == 0, agent.name
