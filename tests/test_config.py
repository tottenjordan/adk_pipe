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


# --- Part C: shared BaseAgentConfiguration + build_infra_retry ---
# The two agents' ResearchConfiguration classes were ~95% identical; both now
# subclass a single BaseAgentConfiguration in agent_common, and both build their
# INFRA_RETRY from one factory (creative_agent adds the genai ServerError).

# Shared model/rate fields that must live on the base (dedup contract).
_SHARED_CONFIG_FIELDS = (
    "critic_model",
    "worker_model",
    "video_analysis_model",
    "lite_planner_model",
    "image_gen_model",
    "video_gen_model",
    "max_results_yt_trends",
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
        from agent_common.config import BaseAgentConfiguration
        import trend_scout.config as tt
        import creative_agent.config as ca

        assert isinstance(tt.config, BaseAgentConfiguration)
        assert isinstance(ca.config, BaseAgentConfiguration)

    def test_agent_configs_share_model_names(self):
        """Dedup proof: both agents expose identical base model-name values."""
        import trend_scout.config as tt
        import creative_agent.config as ca

        for name in (
            "critic_model",
            "worker_model",
            "video_analysis_model",
            "lite_planner_model",
            "image_gen_model",
            "video_gen_model",
        ):
            assert getattr(tt.config, name) == getattr(ca.config, name)
        assert tt.config.critic_model == "gemini-3.1-pro-preview"

    def test_trend_scout_regional_model_spread(self):
        """trend_scout fans its 5 agents across separate quota buckets.

        The two gemini-2.5 agents are pinned to a region (us-central1) so they
        land in the regional per-base-model quota, separate from the global
        buckets the gemini-3.x agents use.
        """
        import trend_scout.config as tt

        assert tt.config.gather_model == "gemini-2.5-flash-lite"
        assert tt.config.picker_model == "gemini-2.5-pro"
        assert tt.config.regional_model_location == "us-central1"

    def test_creative_agent_regional_model_spread(self):
        """creative_agent pins its campaign-research pipeline to a separate
        regional (gemini-2.5 @ us-central1) quota bucket (mirrors trend_scout #94).

        Halves parallel_planner_agent's contention: the trend-research half stays
        on the global flash/flash-lite buckets while the campaign half draws from
        the regional pool instead of doubling up on the same global buckets.
        """
        import creative_agent.config as ca

        assert ca.config.regional_model_location == "us-central1"
        assert ca.config.regional_worker_model == "gemini-2.5-flash"
        assert ca.config.regional_lite_planner_model == "gemini-2.5-flash-lite"
        # the global base-model names must be UNCHANGED (spread != rename)
        assert ca.config.worker_model == "gemini-3.5-flash"
        assert ca.config.lite_planner_model == "gemini-3.1-flash-lite"

    def test_campaign_placement_default_is_regional(self, monkeypatch, fresh_config):
        """Env unset → the shipped #101 behavior: campaign on gemini-2.5 @ us-central1.

        The DoE arm seam must be behavior-preserving by default so prod is
        untouched unless CAMPAIGN_RESEARCH_PLACEMENT is set explicitly.
        """
        monkeypatch.delenv("CAMPAIGN_RESEARCH_PLACEMENT", raising=False)
        ca = fresh_config("creative_agent.config")
        assert ca.config.campaign_models() == (
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
            "us-central1",
        )

    def test_campaign_placement_global_3x_arm(self, monkeypatch, fresh_config):
        """Arm A (global_3x): campaign shares the trend half's global 3.x buckets."""
        monkeypatch.setenv("CAMPAIGN_RESEARCH_PLACEMENT", "global_3x")
        ca = fresh_config("creative_agent.config")
        assert ca.config.campaign_models() == (
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "global",
        )

    def test_campaign_placement_global_altbucket_arm(self, monkeypatch, fresh_config):
        """Arm C (global_altbucket): a DISTINCT global 3.x bucket.

        Task 0a probe confirmed gemini-3-flash-preview both calls and grounds via
        google_search @ global — the one distinct global flash base model, so the
        campaign planner + worker both use it.
        """
        monkeypatch.setenv("CAMPAIGN_RESEARCH_PLACEMENT", "global_altbucket")
        ca = fresh_config("creative_agent.config")
        assert ca.config.campaign_models() == (
            "gemini-3-flash-preview",
            "gemini-3-flash-preview",
            "global",
        )

    def test_campaign_placement_unknown_falls_back_to_regional(
        self, monkeypatch, fresh_config
    ):
        """An unrecognized arm degrades to the safe default (regional)."""
        monkeypatch.setenv("CAMPAIGN_RESEARCH_PLACEMENT", "bogus_arm")
        ca = fresh_config("creative_agent.config")
        assert ca.config.campaign_models() == (
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
            "us-central1",
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
        from agent_common.retry import build_infra_retry
        from google.genai import errors as genai_errors

        rc = build_infra_retry(extra_exceptions=[genai_errors.ServerError])
        names = set(rc.exceptions)
        assert "ServerError" in names
        assert "ServiceUnavailable" in names  # base still present
