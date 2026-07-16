"""Model factory that pins ADK Gemini models to the global-serving location.

See :mod:`agent_common.locations` for why the location must be pinned in code
rather than via the reserved ``GOOGLE_CLOUD_LOCATION`` env var.

ADK exposes ``Gemini.client_kwargs`` for exactly this: extra kwargs passed
straight to the underlying ``google.genai.Client`` constructor, where an explicit
``location`` takes precedence over the injected ``GOOGLE_CLOUD_LOCATION``.
"""

from google.adk.models import Gemini

from agent_common import locations


def build_gemini(model_name: str, location: str | None = None) -> Gemini:
    """Return an ADK ``Gemini`` model pinned to a model-serving location.

    Use this everywhere an agent would otherwise take a bare model-name string,
    so gemini-3.x models resolve to their `global`-serving endpoint even inside a
    regional Agent Engine deployment.

    ``location`` defaults to :data:`agent_common.locations.MODEL_LOCATION`
    (``global``, the only endpoint that serves gemini-3.x). Pass an explicit
    region (e.g. ``us-central1``) for models that are served regionally — this
    also lands their calls in the *regional* per-base-model quota bucket, which
    is a separate pool from the ``global`` one the gemini-3.x models draw from.
    """
    return Gemini(
        model=model_name,
        client_kwargs={"location": location or locations.MODEL_LOCATION},
    )
