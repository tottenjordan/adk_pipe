"""Shared pytest fixtures (test doubles live in ``tests/_fakes.py``)."""

import importlib
import os
import sys
from pathlib import Path

import pytest

# Persistent JAX compilation cache: XLA compiles were ~58% of the bandit tests'
# time, and most programs are identical run to run. JAX reads each `jax_*`
# option's default from its `JAX_*` env var when it is first imported, so
# setting the env here (before any test module is collected) configures the
# cache without importing jax for non-bandit runs. Being plain env vars, they
# are also inherited by subprocesses (e.g. the staged-predictor child in
# test_build_image.py). Any value already in the environment wins, so CI or a
# developer can point JAX_COMPILATION_CACHE_DIR elsewhere. The cache lives
# under .pytest_cache/ (gitignored; `pytest --cache-clear` does not touch it,
# delete the directory to reset).
os.environ.setdefault(
    "JAX_COMPILATION_CACHE_DIR",
    str(Path(__file__).resolve().parent.parent / ".pytest_cache" / "jax"),
)
# Cache every program, however quick to compile or small (JAX's defaults skip
# compiles under 1 s, which is most of ours).
os.environ.setdefault("JAX_PERSISTENT_CACHE_MIN_COMPILE_TIME_SECS", "0")
os.environ.setdefault("JAX_PERSISTENT_CACHE_MIN_ENTRY_SIZE_BYTES", "0")


@pytest.fixture
def gcp_project_env(monkeypatch):
    """Pin a dummy ``GOOGLE_CLOUD_PROJECT`` (and clear the project number) so
    config read at use time doesn't depend on the caller's shell/.env (CI sets
    only the project)."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_NUMBER", raising=False)


@pytest.fixture
def image_qa_off(monkeypatch):
    """Disable post-render image QA (on by default) for tests that drive
    ``generate_image`` with a fake image client and count its calls; the QA
    flow itself is covered in ``tests/test_image_qa.py``."""
    from creative_agent.config import config

    monkeypatch.setattr(config, "image_qa_enabled", False)


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
