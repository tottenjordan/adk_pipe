"""Config resolution tests.

Regression for the deployed-only bug where `creative_agent` resolved its GCS
bucket name from a `GCS_BUCKET_NAME` env var that `deployment/deploy_agent.py`
never passes to Agent Engine (it passes `GOOGLE_CLOUD_STORAGE_BUCKET`). Locally
`.env` defines `GCS_BUCKET_NAME`, so it worked; deployed it was `None`, which
surfaced as `ValueError: Cannot determine path without bucket name.`

`trend_trawler` already reads `GOOGLE_CLOUD_STORAGE_BUCKET`; both agents must
resolve the bucket name from the same env var that deploy actually ships.

Config values are read at class-definition (import) time, so these tests import
the config module fresh under a patched environment. `importlib.reload` would
mutate the *already-imported* module in place and break identity checks elsewhere
(e.g. `agent.retry_config is INFRA_RETRY`), so instead we swap the module out of
`sys.modules`, import a throwaway copy, and restore the original in teardown.
"""

import importlib
import sys

import pytest

# Importing `<pkg>.config` also runs `<pkg>/__init__.py`, which imports the agent
# module (binding its own INFRA_RETRY). So a fresh config import has side effects
# across the whole package; we snapshot and fully restore this module subset to
# avoid leaving agents bound to a stale config (which breaks `is INFRA_RETRY`).
_PKG_PREFIXES = (
    "creative_agent",
    "trend_trawler",
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


def test_creative_agent_bucket_name_reads_storage_bucket_var(monkeypatch, fresh_config):
    """creative_agent must resolve GCS_BUCKET_NAME from GOOGLE_CLOUD_STORAGE_BUCKET."""
    # Diverging sentinels: correct code reads GOOGLE_CLOUD_STORAGE_BUCKET, the old
    # buggy code read GCS_BUCKET_NAME. load_dotenv(override=False) won't clobber
    # these already-set values, so the assertion cleanly distinguishes the two.
    monkeypatch.setenv("GOOGLE_CLOUD_STORAGE_BUCKET", "correct-bucket")
    monkeypatch.setenv("GCS_BUCKET_NAME", "wrong-bucket")

    ca_config = fresh_config("creative_agent.config")
    assert ca_config.config.GCS_BUCKET_NAME == "correct-bucket"


def test_both_agents_resolve_bucket_name_from_same_var(monkeypatch, fresh_config):
    """trend_trawler and creative_agent must agree on the bucket-name env var."""
    monkeypatch.setenv("GOOGLE_CLOUD_STORAGE_BUCKET", "shared-bucket")
    monkeypatch.setenv("GCS_BUCKET_NAME", "stale-bucket")

    ca_config = fresh_config("creative_agent.config")
    tt_config = fresh_config("trend_trawler.config")
    assert ca_config.config.GCS_BUCKET_NAME == tt_config.config.GCS_BUCKET_NAME
    assert ca_config.config.GCS_BUCKET_NAME == "shared-bucket"
