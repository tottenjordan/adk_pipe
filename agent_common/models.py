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
"""

from google.adk.models import FallbackModel, Gemini

from agent_common import genai_retry, locations

# ~10s of HTTP backoff on the primary before failing over, not the full ~130s.
PRIMARY_FAILOVER_ATTEMPTS = 2


def build_gemini(
    model_name: str, location: str | None = None, retry_attempts: int | None = None
) -> Gemini:
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
    return Gemini(
        model=model_name,
        retry_options=retry,
        client_kwargs={"location": location or locations.MODEL_LOCATION},
    )


def build_gemini_with_fallback(
    primary: str, fallback: str | None
) -> Gemini | FallbackModel:
    """Pro-quota-bound producer model: ``primary``, then ``fallback`` on 429/5xx.

    Both delegates are :func:`build_gemini` instances — a bare model-name string
    in ``FallbackModel.models`` is resolved via ``LLMRegistry`` and would lose the
    global location pin. The primary gets a short HTTP retry so it fails over
    fast; the backup keeps the full quota-paced retry. An empty ``fallback``
    (the ``CRITIC_FALLBACK_MODEL=""`` kill switch) returns the plain pinned model.
    """
    if not fallback:
        return build_gemini(primary)
    return FallbackModel(
        models=[
            build_gemini(primary, retry_attempts=PRIMARY_FAILOVER_ATTEMPTS),
            build_gemini(fallback),
        ]
    )
