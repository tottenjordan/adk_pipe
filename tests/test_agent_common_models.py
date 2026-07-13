"""Tests for the shared model location + factory (agent_common).

gemini-3.x models are served ONLY from the `global` Vertex location. Inside a
deployed Agent Engine the `GOOGLE_CLOUD_LOCATION` env var is reserved and
auto-injected as the engine's region (us-central1), which 404s these models. The
model client location is therefore pinned in code via `build_gemini`, driven by
the NON-reserved `MODEL_LOCATION` (default `global`) so it survives deploys.
"""

import importlib


def test_model_location_defaults_to_global(monkeypatch):
    """MODEL_LOCATION must default to `global` when the env var is unset."""
    monkeypatch.delenv("MODEL_LOCATION", raising=False)
    import agent_common.locations as locations

    importlib.reload(locations)
    assert locations.MODEL_LOCATION == "global"


def test_build_gemini_pins_location(monkeypatch):
    """build_gemini returns an ADK Gemini pinned to the model location."""
    monkeypatch.delenv("MODEL_LOCATION", raising=False)
    import agent_common.locations as locations

    importlib.reload(locations)
    from agent_common.models import build_gemini

    gem = build_gemini("gemini-3.1-pro-preview")
    assert gem.model == "gemini-3.1-pro-preview"
    assert gem.client_kwargs == {"location": "global"}


def test_model_location_env_override(monkeypatch):
    """MODEL_LOCATION honours an explicit non-reserved override; build_gemini follows."""
    monkeypatch.setenv("MODEL_LOCATION", "us-central1")
    import agent_common.locations as locations

    importlib.reload(locations)
    from agent_common.models import build_gemini

    assert locations.MODEL_LOCATION == "us-central1"
    assert build_gemini("m").client_kwargs == {"location": "us-central1"}

    # restore module state for other tests
    monkeypatch.delenv("MODEL_LOCATION", raising=False)
    importlib.reload(locations)
