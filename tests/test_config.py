"""Config resolution tests.

Regression for the deployed-only bug where `creative_agent` resolved its GCS
bucket name from a `GCS_BUCKET_NAME` env var that `deployment/deploy_agent.py`
never passes to Agent Engine (it passes `GOOGLE_CLOUD_STORAGE_BUCKET`). Locally
`.env` defines `GCS_BUCKET_NAME`, so it worked; deployed it was `None`, which
surfaced as `ValueError: Cannot determine path without bucket name.`

`trend_scout` already reads `GOOGLE_CLOUD_STORAGE_BUCKET`; both agents must
resolve the bucket name from the same env var that deploy actually ships.

Config values are read at class-definition (import) time, so these tests import
the config module fresh under a patched environment. `importlib.reload` would
mutate the *already-imported* module in place and break identity checks elsewhere
(e.g. `agent.retry_config is INFRA_RETRY`), so instead we swap the module out of
`sys.modules`, import a throwaway copy, and restore the original in teardown.
"""

import importlib
import logging
import sys

import pytest

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
    """trend_scout and creative_agent must agree on the bucket-name env var."""
    monkeypatch.setenv("GOOGLE_CLOUD_STORAGE_BUCKET", "shared-bucket")
    monkeypatch.setenv("GCS_BUCKET_NAME", "stale-bucket")

    ca_config = fresh_config("creative_agent.config")
    tt_config = fresh_config("trend_scout.config")
    assert ca_config.config.GCS_BUCKET_NAME == tt_config.config.GCS_BUCKET_NAME
    assert ca_config.config.GCS_BUCKET_NAME == "shared-bucket"


def test_gcs_bucket_uri_derived_from_bucket_name(monkeypatch, fresh_config):
    """GCS_BUCKET is the gs:// form of GOOGLE_CLOUD_STORAGE_BUCKET; BUCKET is ignored."""
    monkeypatch.setenv("GOOGLE_CLOUD_STORAGE_BUCKET", "derived-bucket")
    monkeypatch.setenv("BUCKET", "gs://stale-separate-var")

    base = fresh_config("agent_common.config").BaseAgentConfiguration
    assert base.GCS_BUCKET == "gs://derived-bucket"
    ca_config = fresh_config("creative_agent.config")
    tt_config = fresh_config("trend_scout.config")
    assert ca_config.config.GCS_BUCKET == "gs://derived-bucket"
    assert tt_config.config.GCS_BUCKET == "gs://derived-bucket"


def test_gcs_bucket_uri_none_when_bucket_name_unset(monkeypatch, fresh_config):
    """No bucket name -> GCS_BUCKET is None (not the literal "gs://None")."""
    # Empty (not deleted) so load_dotenv(override=False) can't refill it from .env.
    monkeypatch.setenv("GOOGLE_CLOUD_STORAGE_BUCKET", "")

    base = fresh_config("agent_common.config").BaseAgentConfiguration
    assert base.GCS_BUCKET is None


# --- Part C: shared BaseAgentConfiguration + build_infra_retry ---
# The two agents' ResearchConfiguration classes were ~95% identical; both now
# subclass a single BaseAgentConfiguration in agent_common, and both build their
# INFRA_RETRY from one factory (creative_agent adds the genai ServerError).

# Shared model/rate fields that must live on the base (dedup contract).
_SHARED_CONFIG_FIELDS = (
    "critic_model",
    "worker_model",
    "lite_planner_model",
    "image_gen_model",
    "rate_limit_seconds",
    "rpm_quota",
    "GCS_BUCKET",
    "GCS_BUCKET_NAME",
    "PROJECT_ID",
    "PROJECT_NUMBER",
    "BQ_PROJECT_ID",
    "BQ_DATASET_ID",
    "BQ_TABLE_TARGETS",
    "BQ_TABLE_CREATIVES",
    "BQ_TABLE_ALL_TRENDS",
)


class TestBaseAgentConfiguration:
    def test_base_exists_with_shared_fields(self):
        from agent_common.config import BaseAgentConfiguration

        base = BaseAgentConfiguration()
        for name in _SHARED_CONFIG_FIELDS:
            assert hasattr(base, name), f"BaseAgentConfiguration missing {name}"

    def test_both_agent_configs_subclass_the_base(self):
        import creative_agent.config as ca
        import trend_scout.config as tt
        from agent_common.config import BaseAgentConfiguration

        assert isinstance(tt.config, BaseAgentConfiguration)
        assert isinstance(ca.config, BaseAgentConfiguration)

    def test_agent_configs_share_model_names(self):
        """Dedup proof: both agents expose identical base model-name values."""
        import creative_agent.config as ca
        import trend_scout.config as tt

        for name in (
            "critic_model",
            "worker_model",
            "lite_planner_model",
            "image_gen_model",
        ):
            assert getattr(tt.config, name) == getattr(ca.config, name)
        assert tt.config.critic_model == "gemini-3.1-pro-preview"

    def test_trend_scout_model_spread(self):
        """trend_scout fans its 5 agents across 5 distinct base-model buckets."""
        import trend_scout.config as tt

        assert tt.config.gather_model == "gemini-3.1-flash-lite"
        assert tt.config.picker_model == "gemini-3.5-flash"
        assert not hasattr(tt.config, "regional_model_location")
        buckets = {
            tt.config.worker_model,
            tt.config.lite_planner_model,
            tt.config.critic_model,
            tt.config.gather_model,
            tt.config.picker_model,
        }
        assert len(buckets) == 5

    def test_base_models_are_2026_09_lineup(self):
        """Trend half + shared models: current-gen gemini-3.x (no gemini-2.5)."""
        import creative_agent.config as ca

        assert ca.config.worker_model == "gemini-3.8-flash"
        assert ca.config.lite_planner_model == "gemini-3.5-flash-lite"
        assert ca.config.critic_model == "gemini-3.1-pro-preview"
        assert ca.config.image_gen_model == "gemini-3.1-flash-image"

    def test_regional_25_arm_fields_removed(self):
        """The retired gemini-2.5 campaign arm's fields are gone."""
        import creative_agent.config as ca

        for name in (
            "regional_model_location",
            "regional_worker_model",
            "regional_lite_planner_model",
        ):
            assert not hasattr(ca.config, name)

    def test_campaign_placement_default_is_global_altbucket(
        self, monkeypatch, fresh_config
    ):
        """Env unset → campaign on the distinct global gemini-3.5-flash bucket."""
        monkeypatch.delenv("CAMPAIGN_RESEARCH_PLACEMENT", raising=False)
        ca = fresh_config("creative_agent.config")
        assert ca.config.campaign_research_placement == "global_altbucket"
        assert ca.config.campaign_models() == (
            "gemini-3.5-flash",
            "gemini-3.5-flash",
            "global",
        )

    def test_campaign_placement_global_3x_arm(self, monkeypatch, fresh_config):
        """Arm A (global_3x): campaign shares the trend half's global 3.x buckets."""
        monkeypatch.setenv("CAMPAIGN_RESEARCH_PLACEMENT", "global_3x")
        ca = fresh_config("creative_agent.config")
        assert ca.config.campaign_models() == (
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash",
            "global",
        )

    @pytest.mark.parametrize("arm", ["bogus_arm", "regional_25"])
    def test_campaign_placement_unknown_or_retired_falls_back_to_default(
        self, arm, monkeypatch, fresh_config
    ):
        """An unrecognized (or the retired regional_25) arm → global_altbucket."""
        monkeypatch.setenv("CAMPAIGN_RESEARCH_PLACEMENT", arm)
        ca = fresh_config("creative_agent.config")
        assert ca.config.campaign_models() == (
            "gemini-3.5-flash",
            "gemini-3.5-flash",
            "global",
        )


class TestBuildInfraRetry:
    def test_base_exceptions_present_no_serrver_error(self):
        from agent_common.retry import build_infra_retry

        rc = build_infra_retry()
        names = set(rc.exceptions)  # RetryConfig stores class names as strings
        assert rc.max_attempts == 3
        assert {
            "ServiceUnavailable",
            "InternalServerError",
            "GatewayTimeout",
            "TooManyRequests",
            "DeadlineExceeded",
            "ConnectionError",
            "TimeoutError",
        } <= names
        assert "ServerError" not in names

    def test_extra_exceptions_appended(self):
        from google.genai import errors as genai_errors

        from agent_common.retry import build_infra_retry

        rc = build_infra_retry(extra_exceptions=[genai_errors.ServerError])
        names = set(rc.exceptions)
        assert "ServerError" in names
        assert "ServiceUnavailable" in names  # base still present

    def test_schema_retry_covers_validation_and_infra(self):
        """Structured-output producers additionally retry a bad-JSON
        ValidationError (issue #104) on top of the infra set (defense-in-depth for
        a 503 that escapes the genai HTTP-retry layer)."""
        from creative_agent.config import SCHEMA_RETRY

        names = set(SCHEMA_RETRY.exceptions)
        assert "ValidationError" in names  # the invalid-JSON crash
        assert "ServiceUnavailable" in names  # infra set included (503)
        assert "ServerError" in names  # genai 5xx included


# --- 2026-09 model-lineup refresh: retire gemini-2.5 from the campaign half ---
# Vertex blocks gemini-2.5 for idle projects ~Oct 2026 (shutdown early 2027), so
# the default campaign arm must be off 2.5 while keeping the PR #101 spread.


def test_default_campaign_arm_uses_no_retiring_models(monkeypatch, fresh_config):
    monkeypatch.delenv("CAMPAIGN_RESEARCH_PLACEMENT", raising=False)
    ca = fresh_config("creative_agent.config")

    lite, worker, loc = ca.config.campaign_models()
    assert not lite.startswith("gemini-2.5") and not worker.startswith("gemini-2.5")
    assert loc == "global"


def test_campaign_default_bucket_differs_from_trend_bucket(monkeypatch, fresh_config):
    monkeypatch.delenv("CAMPAIGN_RESEARCH_PLACEMENT", raising=False)
    ca = fresh_config("creative_agent.config")

    cfg = ca.config
    _, worker, _ = cfg.campaign_models()
    assert worker not in {cfg.worker_model, cfg.lite_planner_model}


def test_unknown_campaign_arm_logs_warning(monkeypatch, fresh_config, caplog):
    """A retired/unknown arm warns (naming it + the fallback) before degrading."""
    monkeypatch.setenv("CAMPAIGN_RESEARCH_PLACEMENT", "regional_25")
    ca = fresh_config("creative_agent.config")

    with caplog.at_level(logging.WARNING, logger="creative_agent.config"):
        ca.config.campaign_models()

    assert any(
        r.levelno == logging.WARNING
        and "regional_25" in r.getMessage()
        and "global_altbucket" in r.getMessage()
        for r in caplog.records
    )


def test_trend_scout_config_uses_no_retiring_models():
    """Every *_model on trend_scout's config is off the retiring gemini-2.5 line."""
    import dataclasses

    import trend_scout.config as tt

    models = {
        f.name: getattr(tt.config, f.name)
        for f in dataclasses.fields(tt.config)
        if f.name.endswith("_model")
    }
    assert {"gather_model", "picker_model", "worker_model"} <= models.keys()
    for name, model in models.items():
        assert not model.startswith("gemini-2.5"), f"{name}={model}"
