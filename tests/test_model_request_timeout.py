"""Per-request model timeout (agent_common) — a hung Gemini call must not stall a run.

Incident 2026-10-02: one google_search-grounded research request never returned
and blocked the detached run until RUN_MAX_SECONDS. Every model call now carries a
client-side timeout (genai ``HttpOptions.timeout``, MILLISECONDS) and a timed-out
call is retried a bounded number of times.

Why the agent-model retry lives in ``TimeoutRetryingGemini`` and not in
``HttpRetryOptions``: ADK calls genai's *async* client, which uses aiohttp whenever
it is installed (it is, in prod). An aiohttp timeout surfaces as the builtin
``TimeoutError``, which genai's tenacity predicate (``APIError`` status codes +
httpx timeouts only) does NOT retry. The sync httpx path (judge, image gen) raises
``httpx.ReadTimeout``, which genai's retry does cover.
"""

import asyncio
import importlib

import httpx
import pytest
from google.adk.models import FallbackModel, Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


@pytest.fixture
def genai_retry(monkeypatch):
    """Fresh genai_retry with MODEL_REQUEST_TIMEOUT_SECONDS unset; restored after."""
    import agent_common.genai_retry as mod

    for var in ("MODEL_REQUEST_TIMEOUT_SECONDS", "FLASH_MODEL_REQUEST_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(mod)
    yield mod
    for var in ("MODEL_REQUEST_TIMEOUT_SECONDS", "FLASH_MODEL_REQUEST_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(mod)


# --- the single source-of-truth setting --------------------------------------


def test_timeout_defaults_to_240s(genai_retry):
    assert genai_retry.MODEL_REQUEST_TIMEOUT_SECONDS == 240
    assert genai_retry.model_request_timeout_ms() == 240_000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 240),
        ("", 240),
        ("   ", 240),
        ("not-a-number", 240),
        ("120", 120),
        ("10", 30),  # clamped up
        ("5000", 900),  # clamped down
        ("0", None),  # disabled
        ("-5", None),  # non-positive disables too
    ],
)
def test_resolve_timeout_parses_clamps_and_disables(genai_retry, raw, expected):
    assert genai_retry.resolve_model_request_timeout(raw) == expected


def test_timeout_env_override(genai_retry, monkeypatch):
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "90")
    importlib.reload(genai_retry)
    assert genai_retry.MODEL_REQUEST_TIMEOUT_SECONDS == 90
    assert genai_retry.model_request_timeout_ms() == 90_000


def test_timeout_env_disable(genai_retry, monkeypatch):
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "0")
    importlib.reload(genai_retry)
    assert genai_retry.MODEL_REQUEST_TIMEOUT_SECONDS is None
    assert genai_retry.model_request_timeout_ms() is None


# --- build_gemini carries it -------------------------------------------------


def test_build_gemini_carries_timeout(genai_retry):
    from agent_common.models import TimeoutRetryingGemini, build_gemini

    gem = build_gemini("gemini-3.1-pro-preview")
    assert isinstance(gem, TimeoutRetryingGemini)
    assert isinstance(gem, Gemini)  # still a Gemini for every ADK isinstance check
    assert gem.request_timeout_ms == 240_000
    # location pin + retry_options untouched; no http_options smuggled in.
    assert gem.client_kwargs == {"location": "global"}
    assert gem.retry_options is not None


def test_build_gemini_timeout_disabled(genai_retry, monkeypatch):
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "0")
    importlib.reload(genai_retry)
    from agent_common.models import build_gemini

    assert build_gemini("m").request_timeout_ms is None


def test_fallback_delegates_carry_timeout(genai_retry):
    from agent_common.models import build_gemini_with_fallback

    fb = build_gemini_with_fallback("gemini-3.1-pro-preview", "gemini-3.8-flash")
    assert isinstance(fb, FallbackModel)
    for m in fb.models:
        assert m.client_kwargs == {"location": "global"}
    # Model-aware: the Pro primary keeps the general timeout, the flash backup
    # gets the shorter flash timeout.
    assert [m.request_timeout_ms for m in fb.models] == [240_000, 90_000]


# --- model-aware flash timeout -------------------------------------------------
# Incident 2026-10-08: a Flash call (art director) hung ~4.6 min until Vertex
# returned 504 DEADLINE_EXCEEDED. Flash/lite turns are short, so they get a
# much tighter per-request timeout than Pro thinking turns.


@pytest.mark.parametrize(
    ("model", "is_flash"),
    [
        ("gemini-3.8-flash", True),
        ("gemini-3.5-flash", True),
        ("gemini-3.5-flash-lite", True),
        ("gemini-3.1-flash-lite", True),
        ("GEMINI-3.8-FLASH", True),
        ("some-lite-model", True),
        ("gemini-3.1-pro-preview", False),
        ("gemini-nano-banana-2.1", False),
        ("m", False),
        ("", False),
        (None, False),
    ],
)
def test_is_flash_model(genai_retry, model, is_flash):
    assert genai_retry.is_flash_model(model) is is_flash


def test_flash_timeout_defaults_to_90s(genai_retry):
    assert genai_retry.FLASH_MODEL_REQUEST_TIMEOUT_SECONDS == 90
    assert genai_retry.model_request_timeout_ms("gemini-3.8-flash") == 90_000
    assert genai_retry.model_request_timeout_ms("gemini-3.1-pro-preview") == 240_000
    # No model name = the general timeout (judge / image-gen call sites).
    assert genai_retry.model_request_timeout_ms() == 240_000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 90),
        ("", 90),
        ("junk", 90),
        ("60", 60),
        ("10", 30),  # clamped up
        ("5000", 900),  # clamped down
        ("0", None),  # disabled -> general timeout applies
        ("-1", None),
    ],
)
def test_resolve_flash_timeout(genai_retry, raw, expected):
    assert genai_retry.resolve_flash_model_request_timeout(raw) == expected


def test_flash_timeout_env_override(genai_retry, monkeypatch):
    monkeypatch.setenv("FLASH_MODEL_REQUEST_TIMEOUT_SECONDS", "45")
    importlib.reload(genai_retry)
    assert genai_retry.model_request_timeout_ms("gemini-3.8-flash") == 45_000
    assert genai_retry.model_request_timeout_ms("gemini-3.1-pro-preview") == 240_000


def test_flash_timeout_disabled_falls_back_to_general(genai_retry, monkeypatch):
    monkeypatch.setenv("FLASH_MODEL_REQUEST_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "200")
    importlib.reload(genai_retry)
    assert genai_retry.model_request_timeout_ms("gemini-3.8-flash") == 200_000


def test_flash_timeout_never_longer_than_general(genai_retry, monkeypatch):
    """The flash timeout only ever tightens: a lower general timeout wins."""
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "60")
    importlib.reload(genai_retry)
    assert genai_retry.model_request_timeout_ms("gemini-3.8-flash") == 60_000


def test_flash_timeout_applies_when_general_disabled(genai_retry, monkeypatch):
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "0")
    importlib.reload(genai_retry)
    assert genai_retry.model_request_timeout_ms("gemini-3.8-flash") == 90_000
    assert genai_retry.model_request_timeout_ms("gemini-3.1-pro-preview") is None


def test_build_gemini_flash_gets_flash_timeout(genai_retry):
    from agent_common.models import build_gemini

    assert build_gemini("gemini-3.8-flash").request_timeout_ms == 90_000
    assert build_gemini("gemini-3.5-flash-lite").request_timeout_ms == 90_000
    assert build_gemini("gemini-3.1-pro-preview").request_timeout_ms == 240_000


def test_flash_request_carries_flash_timeout_and_retries(
    genai_retry, monkeypatch, no_sleep
):
    """A flash call that hits the (shorter) client timeout is retried with it."""
    from agent_common.models import build_gemini

    calls = _patch_parent(monkeypatch, {"gemini-3.8-flash": [TimeoutError(), "ok"]})
    assert len(_drain(build_gemini("gemini-3.8-flash"))) == 1
    assert calls == [("gemini-3.8-flash", 90_000)] * 2


def test_vertex_504_is_retried_by_genai_http_retry(genai_retry):
    """X-Server-Timeout makes Vertex answer 504 DEADLINE_EXCEEDED at the client
    timeout; genai's status-code retry must cover it on every model."""
    from agent_common.models import build_gemini, build_gemini_with_fallback

    assert 504 in build_gemini("gemini-3.8-flash").retry_options.http_status_codes
    fb = build_gemini_with_fallback("gemini-3.1-pro-preview", "gemini-3.8-flash")
    for m in fb.models:
        assert 504 in m.retry_options.http_status_codes


# --- generate_content_async behaviour -----------------------------------------


def _patch_parent(monkeypatch, script):
    """Replace Gemini.generate_content_async with a scripted fake.

    ``script`` maps model name -> list of per-call outcomes: an exception to
    raise before yielding, ``("yield_then_raise", exc)``, or ``"ok"``.
    Returns the list of ``(model, timeout_ms)`` calls observed.
    """
    calls = []

    async def fake(self, llm_request, stream=False):
        http = llm_request.config.http_options
        calls.append((self.model, http.timeout if http else None))
        outcome = script[self.model].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, tuple):
            yield LlmResponse()
            raise outcome[1]
        yield LlmResponse()

    monkeypatch.setattr(Gemini, "generate_content_async", fake)
    return calls


def _drain(model, request=None):
    async def run():
        req = request or LlmRequest()
        return [r async for r in model.generate_content_async(req)]

    return asyncio.run(run())


@pytest.fixture
def no_sleep(monkeypatch):
    import agent_common.models as models

    monkeypatch.setattr(models, "TIMEOUT_RETRY_DELAY_SECONDS", 0)


def test_request_gets_timeout_ms(genai_retry, monkeypatch, no_sleep):
    from agent_common.models import build_gemini

    calls = _patch_parent(monkeypatch, {"m": ["ok"]})
    assert len(_drain(build_gemini("m"))) == 1
    assert calls == [("m", 240_000)]


def test_explicit_request_timeout_is_respected(genai_retry, monkeypatch, no_sleep):
    from agent_common.models import build_gemini

    calls = _patch_parent(monkeypatch, {"m": ["ok"]})
    req = LlmRequest(
        config=types.GenerateContentConfig(http_options=types.HttpOptions(timeout=5))
    )
    _drain(build_gemini("m"), req)
    assert calls == [("m", 5)]


def test_timeout_is_retried_then_succeeds(genai_retry, monkeypatch, no_sleep):
    from agent_common.models import build_gemini

    calls = _patch_parent(monkeypatch, {"m": [TimeoutError(), "ok"]})
    assert len(_drain(build_gemini("m"))) == 1
    assert len(calls) == 2


def test_timeout_retries_are_bounded(genai_retry, monkeypatch, no_sleep):
    from agent_common.models import TIMEOUT_RETRY_ATTEMPTS, build_gemini

    outcomes = [TimeoutError() for _ in range(TIMEOUT_RETRY_ATTEMPTS + 2)]
    calls = _patch_parent(monkeypatch, {"m": outcomes})
    with pytest.raises(TimeoutError):
        _drain(build_gemini("m"))
    assert len(calls) == TIMEOUT_RETRY_ATTEMPTS


def test_no_retry_after_partial_output(genai_retry, monkeypatch, no_sleep):
    """Once a response has been yielded the turn is committed: don't splice a retry."""
    from agent_common.models import build_gemini

    calls = _patch_parent(monkeypatch, {"m": [("yield_then_raise", TimeoutError())]})
    with pytest.raises(TimeoutError):
        _drain(build_gemini("m"))
    assert len(calls) == 1


def test_httpx_timeout_left_to_genai_retry(genai_retry, monkeypatch, no_sleep):
    """httpx timeouts are already retried by genai's HttpRetryOptions; no double retry."""
    from agent_common.models import build_gemini

    calls = _patch_parent(monkeypatch, {"m": [httpx.ReadTimeout("t"), "ok"]})
    with pytest.raises(httpx.ReadTimeout):
        _drain(build_gemini("m"))
    assert len(calls) == 1


def test_disabled_timeout_does_not_touch_request(genai_retry, monkeypatch, no_sleep):
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "0")
    importlib.reload(genai_retry)
    from agent_common.models import build_gemini

    calls = _patch_parent(monkeypatch, {"m": ["ok"]})
    _drain(build_gemini("m"))
    assert calls == [("m", None)]


def test_fallback_does_not_fail_over_on_exhausted_timeout(
    genai_retry, monkeypatch, no_sleep
):
    """ADK FallbackModel fails over on 429/5xx only; a status-less TimeoutError
    propagates after the primary's bounded timeout retries (by design)."""
    from agent_common.models import TIMEOUT_RETRY_ATTEMPTS, build_gemini_with_fallback

    calls = _patch_parent(
        monkeypatch,
        {
            "pro": [TimeoutError() for _ in range(TIMEOUT_RETRY_ATTEMPTS)],
            "flash": ["ok"],
        },
    )
    with pytest.raises(TimeoutError):
        _drain(build_gemini_with_fallback("pro", "flash"))
    assert [c[0] for c in calls] == ["pro"] * TIMEOUT_RETRY_ATTEMPTS


# --- direct genai clients (sync httpx path) -----------------------------------


def test_image_qa_client_has_flash_timeout(genai_retry, monkeypatch):
    from creative_agent import image_qa

    captured = {}
    monkeypatch.setattr(image_qa.genai, "Client", lambda **kw: captured.update(kw))
    monkeypatch.setattr(image_qa.config, "image_qa_model", "gemini-3.8-flash")
    image_qa._get_qa_client.cache_clear()
    try:
        image_qa._get_qa_client()
    finally:
        image_qa._get_qa_client.cache_clear()
    assert captured["http_options"].timeout == 90_000
    assert captured["http_options"].retry_options is not None


def test_judge_client_has_timeout(genai_retry, monkeypatch):
    from creative_eval import evaluate as ev
    from creative_eval.config import EvalConfig

    captured = {}
    monkeypatch.setattr(ev.genai, "Client", lambda **kw: captured.update(kw))
    ev._get_client(EvalConfig(project_id="test-project"))
    opts = captured["http_options"]
    assert opts.timeout == 240_000
    assert opts.retry_options is not None  # httpx ReadTimeout retried by genai


def test_image_client_has_timeout(genai_retry, monkeypatch):
    from creative_agent import image_tools

    captured = {}
    monkeypatch.setattr(image_tools.genai, "Client", lambda **kw: captured.update(kw))
    image_tools._get_genai_client.cache_clear()
    try:
        image_tools._get_genai_client()
    finally:
        image_tools._get_genai_client.cache_clear()
    assert captured["http_options"].timeout == 240_000


def test_image_backoff_retries_httpx_timeout():
    from creative_agent import image_tools

    assert image_tools._is_retryable_genai_error(httpx.ReadTimeout("t"))
    assert image_tools._is_retryable_genai_error(httpx.ConnectTimeout("t"))
