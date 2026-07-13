"""Model factory that pins ADK Gemini models to the global-serving location.

See :mod:`agent_common.locations` for why the location must be pinned in code
rather than via the reserved ``GOOGLE_CLOUD_LOCATION`` env var.

ADK exposes ``Gemini.client_kwargs`` for exactly this: extra kwargs passed
straight to the underlying ``google.genai.Client`` constructor, where an explicit
``location`` takes precedence over the injected ``GOOGLE_CLOUD_LOCATION``.
"""

from google.adk.models import Gemini

from agent_common import locations


def build_gemini(model_name: str) -> Gemini:
    """Return an ADK ``Gemini`` model pinned to the model-serving location.

    Use this everywhere an agent would otherwise take a bare model-name string,
    so gemini-3.x models resolve to their `global`-serving endpoint even inside a
    regional Agent Engine deployment.
    """
    return Gemini(
        model=model_name, client_kwargs={"location": locations.MODEL_LOCATION}
    )
