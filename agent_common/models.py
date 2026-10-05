"""Model factory that pins ADK Gemini models to the global-serving location.

See :mod:`agent_common.locations` for why the location must be pinned in code
rather than via the reserved ``GOOGLE_CLOUD_LOCATION`` env var.

ADK exposes ``Gemini.client_kwargs`` for exactly this: extra kwargs passed
straight to the underlying ``google.genai.Client`` constructor, where an explicit
``location`` takes precedence over the injected ``GOOGLE_CLOUD_LOCATION``.

It also carries the shared genai HTTP-layer retry (:mod:`agent_common.genai_retry`)
so every agent model call retries transient 429/503 (the shared per-minute Vertex
quota) with backoff instead of aborting the run.

:func:`build_gemini_with_fallback` wraps a Pro-quota-bound producer in ADK's
``FallbackModel`` so a 429/5xx on the primary fails over to a flash backup
instead of aborting the run.

Every model is a :class:`TimeoutRetryingGemini`: each request carries the shared
per-request timeout (``MODEL_REQUEST_TIMEOUT_SECONDS``, see
:mod:`agent_common.genai_retry`) and a timed-out request is retried a bounded
number of times, so one hung Vertex call can't stall a run until
``RUN_MAX_SECONDS``.

A root orchestrator's model can also opt into a bounded re-ask on an *empty
turn* (``empty_turn_retries``): a Pro root sometimes answers a long tool result
with ``STOP`` and no text or function call, and ADK then ends the invocation
with the workflow unfinished. The runserver's auto-continue only covers the
async ``/runs`` path; this covers every runner (``adk eval``, Agent Engine/CRF).
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import override

from google.adk.models import FallbackModel, Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from agent_common import genai_retry, locations

logger = logging.getLogger("google_adk." + __name__)

# ~10s of HTTP backoff on the primary before failing over, not the full ~130s.
PRIMARY_FAILOVER_ATTEMPTS = 2

# Total attempts (incl. the first) for a request that hits the client timeout.
# Worst case 3 x 240s = 12 min, inside the 30-min RUN_MAX_SECONDS budget.
TIMEOUT_RETRY_ATTEMPTS = 3
TIMEOUT_RETRY_DELAY_SECONDS = 2.0

# Re-asks of an empty turn for the root orchestrators (each costs one prompt's
# input tokens; the runserver auto-continue remains the backstop on exhaustion).
ROOT_EMPTY_TURN_RETRIES = 2


def is_empty_turn(response: LlmResponse) -> bool:
    """True for a clean (``STOP``/unset, no error) turn with nothing to act on.

    "Nothing" = no non-thought text and no function call. Abnormal finishes
    (MAX_TOKENS, SAFETY, MALFORMED_FUNCTION_CALL, ...) and errors are NOT empty
    turns — re-asking would not help those, so they pass through unchanged.
    """
    if response.partial or response.error_code:
        return False
    if response.finish_reason not in (None, types.FinishReason.STOP):
        return False
    parts = (
        response.content.parts if response.content and response.content.parts else []
    )
    return not any((p.text and not p.thought) or p.function_call for p in parts)


class TimeoutRetryingGemini(Gemini):
    """ADK ``Gemini`` with a per-request timeout and bounded retry on timeout.

    ADK's ``Gemini`` has no timeout field, and an ``http_options`` in
    ``client_kwargs`` would clobber ADK's tracking headers + ``retry_options``. So
    the timeout is set per request on ``llm_request.config.http_options.timeout``
    (MILLISECONDS), which genai merges over the client's options for that call
    (and also forwards to Vertex as ``X-Server-Timeout``).

    The retry must live here: ADK uses genai's *async* client, which uses aiohttp
    when installed (it is, in prod), and an aiohttp timeout surfaces as the
    builtin ``TimeoutError`` — which genai's ``HttpRetryOptions`` predicate
    (``APIError`` status codes + httpx timeouts) does NOT retry. httpx timeouts
    (the no-aiohttp path) are left to genai's retry to avoid double-retrying.

    A retry happens only before the first response is yielded; after that the
    turn is committed to this attempt (same rule as ADK's ``FallbackModel``).
    An exhausted ``TimeoutError`` carries no HTTP status, so a ``FallbackModel``
    wrapping this does NOT fail over on it — it propagates (ADK node
    ``RetryConfig`` lists ``TimeoutError`` where an agent sets ``retry_config``).
    """

    request_timeout_ms: int | None = None
    """Per-request client timeout in MILLISECONDS; ``None`` = no timeout."""

    empty_turn_retries: int = 0
    """Re-asks of an empty non-streaming turn (see :func:`is_empty_turn`).

    ``0`` (default) = pass every response through. The last attempt's response
    is always returned as-is, so exhaustion behaves exactly like no retry.
    Streaming calls are never buffered and so never re-asked.
    """

    @override
    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        retries_left = 0 if stream else self.empty_turn_retries
        while retries_left > 0:
            responses = [
                r async for r in self._generate_with_timeout_retry(llm_request, stream)
            ]
            if not responses or not is_empty_turn(responses[-1]):
                for response in responses:
                    yield response
                return
            retries_left -= 1
            logger.warning(
                "Model %s returned an empty turn (finish_reason=%s, no text or "
                "function call); re-asking (%d retr%s left after this)",
                self.model,
                responses[-1].finish_reason,
                retries_left,
                "y" if retries_left == 1 else "ies",
            )
        async for response in self._generate_with_timeout_retry(llm_request, stream):
            yield response

    async def _generate_with_timeout_retry(
        self, llm_request: LlmRequest, stream: bool
    ) -> AsyncGenerator[LlmResponse]:
        if self.request_timeout_ms:
            if llm_request.config.http_options is None:
                llm_request.config.http_options = types.HttpOptions()
            if llm_request.config.http_options.timeout is None:
                llm_request.config.http_options.timeout = self.request_timeout_ms
        for attempt in range(1, TIMEOUT_RETRY_ATTEMPTS + 1):
            yielded = False
            try:
                async for response in super().generate_content_async(
                    llm_request, stream
                ):
                    yielded = True
                    yield response
                return
            except TimeoutError:
                if yielded or attempt >= TIMEOUT_RETRY_ATTEMPTS:
                    raise
                logger.warning(
                    "Model %s request timed out after %sms (attempt %d/%d); retrying",
                    self.model,
                    self.request_timeout_ms,
                    attempt,
                    TIMEOUT_RETRY_ATTEMPTS,
                )
                await asyncio.sleep(TIMEOUT_RETRY_DELAY_SECONDS)


def build_gemini(
    model_name: str,
    location: str | None = None,
    retry_attempts: int | None = None,
    empty_turn_retries: int = 0,
) -> TimeoutRetryingGemini:
    """Return an ADK ``Gemini`` model pinned to a model-serving location.

    Use this everywhere an agent would otherwise take a bare model-name string,
    so gemini-3.x models resolve to their `global`-serving endpoint even inside a
    regional Agent Engine deployment.

    ``location`` defaults to :data:`agent_common.locations.MODEL_LOCATION`
    (``global``, the only endpoint that serves gemini-3.x). Pass an explicit
    region (e.g. ``us-central1``) for models that are served regionally — this
    also lands their calls in the *regional* per-base-model quota bucket, which
    is a separate pool from the ``global`` one the gemini-3.x models draw from.

    ``retry_attempts`` overrides the HTTP retry's attempt count (default: the
    full quota-paced retry of :func:`agent_common.genai_retry.build_genai_http_retry`).

    The model carries the shared per-request timeout
    (:func:`agent_common.genai_retry.model_request_timeout_ms`, MILLISECONDS) and
    retries a timed-out request — see :class:`TimeoutRetryingGemini`.

    ``empty_turn_retries`` re-asks an empty turn (root orchestrators pass
    :data:`ROOT_EMPTY_TURN_RETRIES`); the default ``0`` disables it.
    """
    # retry_options goes on the top-level param (NOT inside client_kwargs): ADK
    # merges it into the client's own http_options alongside its tracking
    # headers, whereas an http_options passed via client_kwargs would overwrite
    # those wholesale.
    retry = (
        genai_retry.build_genai_http_retry()
        if retry_attempts is None
        else genai_retry.build_genai_http_retry(attempts=retry_attempts)
    )
    return TimeoutRetryingGemini(
        model=model_name,
        retry_options=retry,
        client_kwargs={"location": location or locations.MODEL_LOCATION},
        request_timeout_ms=genai_retry.model_request_timeout_ms(),
        empty_turn_retries=empty_turn_retries,
    )


def build_gemini_with_fallback(
    primary: str, fallback: str | None, empty_turn_retries: int = 0
) -> Gemini | FallbackModel:
    """Pro-quota-bound producer model: ``primary``, then ``fallback`` on 429/5xx.

    Both delegates are :func:`build_gemini` instances — a bare model-name string
    in ``FallbackModel.models`` is resolved via ``LLMRegistry`` and would lose the
    global location pin. The primary gets a short HTTP retry so it fails over
    fast; the backup keeps the full quota-paced retry. An empty ``fallback``
    (the ``CRITIC_FALLBACK_MODEL=""`` kill switch) returns the plain pinned model.

    ``empty_turn_retries`` is applied to both delegates (an empty turn is a
    successful response, so ``FallbackModel`` itself never fails over on it).
    """
    if not fallback:
        return build_gemini(primary, empty_turn_retries=empty_turn_retries)
    return FallbackModel(
        models=[
            build_gemini(
                primary,
                retry_attempts=PRIMARY_FAILOVER_ATTEMPTS,
                empty_turn_retries=empty_turn_retries,
            ),
            build_gemini(fallback, empty_turn_retries=empty_turn_retries),
        ]
    )
