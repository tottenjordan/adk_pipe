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
    # location stays in client_kwargs; retry_options must NOT be smuggled in as an
    # http_options here (that would clobber ADK's tracking headers).
    assert gem.client_kwargs == {"location": "global"}
    assert "http_options" not in gem.client_kwargs


def test_build_gemini_sets_http_retry(monkeypatch):
    """Every agent model call carries the HTTP-layer retry for transient 429/503."""
    monkeypatch.delenv("MODEL_LOCATION", raising=False)
    import agent_common.locations as locations

    importlib.reload(locations)
    from agent_common.models import build_gemini

    gem = build_gemini("gemini-3.5-flash")
    assert gem.retry_options is not None
    codes = set(gem.retry_options.http_status_codes)
    assert {429, 503} <= codes
    # permanent request errors must still fail fast, not retry
    assert codes.isdisjoint({400, 403, 404})


def test_build_genai_http_retry_scoped_to_transient():
    """The shared genai HTTP-retry retries quota/transient codes, not permanent 4xx."""
    from agent_common.genai_retry import build_genai_http_retry

    opts = build_genai_http_retry()
    codes = set(opts.http_status_codes)
    assert {429, 500, 503, 504} <= codes
    assert codes.isdisjoint({400, 403, 404})
    assert opts.attempts >= 2


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
