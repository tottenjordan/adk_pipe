"""Shared pytest fixtures (test doubles live in ``tests/_fakes.py``)."""

import importlib
import sys

import pytest


@pytest.fixture
def gcp_project_env(monkeypatch):
    """Pin a dummy ``GOOGLE_CLOUD_PROJECT`` (and clear the project number) so
    config read at use time doesn't depend on the caller's shell/.env (CI sets
    only the project)."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_NUMBER", raising=False)


# Importing `<pkg>.config` also runs `<pkg>/__init__.py`, which imports the agent
# module (binding its own INFRA_RETRY). So a fresh config import has side effects
# across the whole package; we snapshot and fully restore this module subset to
# avoid leaving agents bound to a stale config (which breaks `is INFRA_RETRY`).
_PKG_PREFIXES = (
    "creative_agent",
    "trend_scout",
    "creative_eval",
    "interactive_creative",
    "agent_common",
)


def _relevant_modules():
    return [name for name in sys.modules if name.startswith(_PKG_PREFIXES)]


@pytest.fixture
def fresh_config():
    """Import config modules fresh under patched env, fully rolled back after."""
    saved = {name: sys.modules[name] for name in _relevant_modules()}

    def _import(name):
        # Drop every cached copy so agent + config re-import together against the
        # patched env (keeping their INFRA_RETRY identities mutually consistent).
        for cached in _relevant_modules():
            del sys.modules[cached]
        return importlib.import_module(name)

    try:
        yield _import
    finally:
        for cached in _relevant_modules():
            del sys.modules[cached]
        sys.modules.update(saved)
