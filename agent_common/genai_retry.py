"""Factory for the shared google-genai HTTP-layer retry options.

This is the STATUS-CODE-based sibling of :mod:`agent_common.retry`. That module
configures ADK's node-level ``RetryConfig``, which matches by *exact exception
class name* — useless for genai 429s, because genai raises every 4xx (400/403/404
*and* 429) as the single class ``google.genai.errors.ClientError``, so its name
can't distinguish a transient quota error from a permanent bad request.

The genai SDK's own HTTP retry keys off the response **status code** instead, so
it can retry transient ``429`` (RESOURCE_EXHAUSTED / the shared per-minute Vertex
quota) and ``503``/``500``/``504`` with exponential backoff while letting the
permanent 4xx (``400``/``403``/``404``) fail fast. Wired via
``Gemini(retry_options=...)`` (ADK merges it into the client's ``http_options``)
for every agent model call, and via a direct ``HttpOptions`` on the standalone
``creative_eval`` judge client.

It also owns the per-request model timeout (``MODEL_REQUEST_TIMEOUT_SECONDS``),
the single source of truth applied to every agent model call (via
:class:`agent_common.models.TimeoutRetryingGemini`), the ``creative_eval`` judge
client and the image-gen client. genai's ``HttpOptions.timeout`` is in
MILLISECONDS — use :func:`model_request_timeout_ms`.

The timeout is model-aware: flash / lite models (:func:`is_flash_model`) get the
much shorter ``FLASH_MODEL_REQUEST_TIMEOUT_SECONDS`` (default 90) — their turns
are short, so a hung flash call is cut and retried within ~1.5 min instead of
4 min. Pass the model name to :func:`model_request_timeout_ms` to get it.

Deliberately free of any ``google.adk`` import so the ADK-free ``creative_eval``
pipeline can share it (mirrors :mod:`agent_common.locations`).
"""

import os

from google.genai import types

# Per-request model timeout. Incident 2026-10-02: one google_search-grounded
# research call never returned and stalled a detached run until RUN_MAX_SECONDS.
# 240s comfortably covers a healthy Pro thinking turn; 0 (or negative) disables.
DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS = 240
MIN_MODEL_REQUEST_TIMEOUT_SECONDS = 30
MAX_MODEL_REQUEST_TIMEOUT_SECONDS = 900


def resolve_model_request_timeout(raw: str | None) -> int | None:
    """Parse ``MODEL_REQUEST_TIMEOUT_SECONDS``: default 240, clamp 30..900, <=0 off.

    Blank / unparseable values fall back to the default rather than disabling the
    timeout (a typo must not silently re-open the hang).
    """
    if raw is None or not raw.strip():
        return DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS
    if value <= 0:
        return None
    return max(
        MIN_MODEL_REQUEST_TIMEOUT_SECONDS, min(MAX_MODEL_REQUEST_TIMEOUT_SECONDS, value)
    )


# Read at import: for Agent Engine the deployer's env is baked into the pickled
# models; on Cloud Run it is read at service start.
MODEL_REQUEST_TIMEOUT_SECONDS = resolve_model_request_timeout(
    os.environ.get("MODEL_REQUEST_TIMEOUT_SECONDS")
)


# Flash / lite per-request timeout. Incident 2026-10-08: a flash call (the art
# director) hung ~4.6 min until Vertex answered 504 DEADLINE_EXCEEDED and the
# retry then succeeded in seconds. Healthy flash turns finish well inside 90s.
DEFAULT_FLASH_MODEL_REQUEST_TIMEOUT_SECONDS = 90


def resolve_flash_model_request_timeout(raw: str | None) -> int | None:
    """Parse ``FLASH_MODEL_REQUEST_TIMEOUT_SECONDS``: default 90, clamp 30..900.

    ``<=0`` returns ``None``, meaning "no flash-specific timeout" — flash models
    then use the general ``MODEL_REQUEST_TIMEOUT_SECONDS``. Blank / unparseable
    values fall back to the default.
    """
    if raw is None or not raw.strip():
        return DEFAULT_FLASH_MODEL_REQUEST_TIMEOUT_SECONDS
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_FLASH_MODEL_REQUEST_TIMEOUT_SECONDS
    if value <= 0:
        return None
    return max(
        MIN_MODEL_REQUEST_TIMEOUT_SECONDS, min(MAX_MODEL_REQUEST_TIMEOUT_SECONDS, value)
    )


FLASH_MODEL_REQUEST_TIMEOUT_SECONDS = resolve_flash_model_request_timeout(
    os.environ.get("FLASH_MODEL_REQUEST_TIMEOUT_SECONDS")
)


def is_flash_model(model_name: str | None) -> bool:
    """True for flash / lite models (name contains ``flash`` or ``lite``)."""
    name = (model_name or "").lower()
    return "flash" in name or "lite" in name


def model_request_timeout_ms(model_name: str | None = None) -> int | None:
    """The per-request timeout in MILLISECONDS (genai ``HttpOptions.timeout``).

    A flash / lite ``model_name`` gets ``FLASH_MODEL_REQUEST_TIMEOUT_SECONDS``
    (never longer than the general timeout when that is set); Pro, other models
    and ``None`` (e.g. image generation, whose renders can be slow) get
    ``MODEL_REQUEST_TIMEOUT_SECONDS``. ``None`` result = no timeout.
    """
    seconds = MODEL_REQUEST_TIMEOUT_SECONDS
    if is_flash_model(model_name) and FLASH_MODEL_REQUEST_TIMEOUT_SECONDS is not None:
        seconds = (
            FLASH_MODEL_REQUEST_TIMEOUT_SECONDS
            if seconds is None
            else min(seconds, FLASH_MODEL_REQUEST_TIMEOUT_SECONDS)
        )
    if seconds is None:
        return None
    return seconds * 1000


# Transient HTTP statuses worth retrying. 429 is the load-bearing one: the
# gemini base models are capped at a few RPM project-wide/shared, so concurrent
# bursts across the pipeline trip 429 RESOURCE_EXHAUSTED. 500/503/504 cover
# transient server/availability blips. Permanent 4xx (400/403/404) are
# intentionally EXCLUDED so genuine request errors still surface immediately.
RETRYABLE_HTTP_STATUS_CODES = [429, 500, 503, 504]


def build_genai_http_retry(
    attempts: int = 5,
    initial_delay: float = 10.0,
    max_delay: float = 60.0,
) -> types.HttpRetryOptions:
    """Return ``HttpRetryOptions`` retrying transient/quota errors with backoff.

    Defaults are tuned for the shared per-minute Vertex quota: a ~10s initial
    delay doubling to a 60s cap gives the bucket time to refill between attempts
    (10 → 20 → 40 → 60 → 60), so a run paced just over quota self-heals instead
    of aborting mid-pipeline.

    Args:
        attempts: total attempts, including the original request.
        initial_delay: seconds before the first retry.
        max_delay: cap on the per-retry delay.
    """
    return types.HttpRetryOptions(
        attempts=attempts,
        initial_delay=initial_delay,
        max_delay=max_delay,
        exp_base=2,
        http_status_codes=RETRYABLE_HTTP_STATUS_CODES,
    )
