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

Deliberately free of any ``google.adk`` import so the ADK-free ``creative_eval``
pipeline can share it (mirrors :mod:`agent_common.locations`).
"""

from google.genai import types

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
