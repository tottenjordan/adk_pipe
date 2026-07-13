"""Tests for deployment utility functions (deploy_agent.py)."""

import os
import sys
import pytest
import dotenv


# --- update_env_file ---
# Replicate the function to avoid module-level vertexai.Client() import
def update_env_file(prefix: str, agent_engine_id: str, env_file_path: str):
    """Updates the .env file with the agent engine ID."""
    KEY_NAME = f"{prefix}_AGENT_ENGINE_ID"
    dotenv.set_key(env_file_path, KEY_NAME, agent_engine_id)


class TestUpdateEnvFile:
    def test_writes_trawler_key(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        update_env_file("TRAWLER", "12345", str(env_file))
        content = env_file.read_text()
        assert "TRAWLER_AGENT_ENGINE_ID" in content
        assert "12345" in content

    def test_writes_creative_key(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        update_env_file("CREATIVE", "67890", str(env_file))
        content = env_file.read_text()
        assert "CREATIVE_AGENT_ENGINE_ID" in content
        assert "67890" in content

    def test_overwrites_existing_value(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('TRAWLER_AGENT_ENGINE_ID="old_id"\n')
        update_env_file("TRAWLER", "new_id", str(env_file))
        content = env_file.read_text()
        assert "new_id" in content
        assert "old_id" not in content

    def test_preserves_other_keys(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('SOME_OTHER_KEY="keep_me"\n')
        update_env_file("TRAWLER", "12345", str(env_file))
        content = env_file.read_text()
        assert "SOME_OTHER_KEY" in content
        assert "keep_me" in content
        assert "TRAWLER_AGENT_ENGINE_ID" in content


# --- ENV_VAR_DICT keys ---
EXPECTED_ENV_VAR_KEYS = [
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT_NUMBER",
    "GOOGLE_CLOUD_STORAGE_BUCKET",
    "BUCKET",
    "BQ_PROJECT_ID",
    "BQ_DATASET_ID",
    "BQ_TABLE_TARGETS",
    "BQ_TABLE_CREATIVES",
    "BQ_TABLE_ALL_TRENDS",
]


class TestEnvVarDict:
    def test_all_expected_keys_present(self):
        """Verify the deploy script's ENV_VAR_DICT includes all required keys."""
        # We can't import deploy_agent.py directly (module-level vertexai.Client),
        # so we verify the expected keys against .env.example
        env_example_path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
        if not os.path.exists(env_example_path):
            pytest.skip(".env.example not found")

        env_values = dotenv.dotenv_values(env_example_path)
        for key in EXPECTED_ENV_VAR_KEYS:
            assert key in env_values, f"Missing {key} in .env.example"

    def test_env_example_has_agent_engine_ids(self):
        """Verify .env.example has placeholder Agent Engine ID fields."""
        env_example_path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
        if not os.path.exists(env_example_path):
            pytest.skip(".env.example not found")

        env_values = dotenv.dotenv_values(env_example_path)
        assert "CREATIVE_AGENT_ENGINE_ID" in env_values
        assert "TRAWLER_AGENT_ENGINE_ID" in env_values

    def test_requirements_file_exists(self):
        """Verify requirements.txt exists (referenced by deploy script)."""
        req_path = os.path.join(os.path.dirname(__file__), "..", "requirements.txt")
        assert os.path.exists(req_path), "requirements.txt not found at project root"

    def test_requirements_includes_adk(self):
        """Verify requirements.txt includes google-adk."""
        req_path = os.path.join(os.path.dirname(__file__), "..", "requirements.txt")
        if not os.path.exists(req_path):
            pytest.skip("requirements.txt not found")
        content = open(req_path).read()
        assert "google-adk" in content


# --- Agent Engine location resolution ---
# Replicate the deploy/test/integration client's location logic to avoid the
# module-level vertexai.Client() import. Agent Engine is a *regional* resource,
# so it must resolve to GCP_REGION (us-central1) — NOT GOOGLE_CLOUD_LOCATION,
# which is `global` for the gemini-3.x models.
def resolve_agent_engine_location() -> str:
    return os.getenv("GCP_REGION", "us-central1")


class TestAgentEngineLocation:
    def test_reads_gcp_region(self, monkeypatch):
        monkeypatch.setenv("GCP_REGION", "us-east4")
        assert resolve_agent_engine_location() == "us-east4"

    def test_defaults_to_us_central1(self, monkeypatch):
        monkeypatch.delenv("GCP_REGION", raising=False)
        assert resolve_agent_engine_location() == "us-central1"

    def test_ignores_global_model_location(self, monkeypatch):
        """GOOGLE_CLOUD_LOCATION=global must not leak into the regional client."""
        monkeypatch.delenv("GCP_REGION", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
        assert resolve_agent_engine_location() == "us-central1"

    def test_env_example_defines_gcp_region(self):
        env_example_path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
        if not os.path.exists(env_example_path):
            pytest.skip(".env.example not found")
        env_values = dotenv.dotenv_values(env_example_path)
        assert env_values.get("GCP_REGION") == "us-central1"


# --- Part B: centralized extra_packages mapping ---
# deploy_agent.py is now importable without GCP creds (lazy vertexai.Client via
# _get_client), so we assert on the REAL mapping/specs rather than a replica.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _import_deploy_agent():
    """Import deploy_agent.py, skipping if its (non-cred) deps are unavailable."""
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    try:
        import deployment.deploy_agent as deploy_agent
    except ImportError as e:  # e.g. vertexai/absl missing in a bare env
        pytest.skip(f"deploy_agent import unavailable: {e}")
    return deploy_agent


class TestAgentExtraPackages:
    def test_creative_agent_bundles_sibling_deps(self):
        """creative_agent imports creative_eval + agent_common → must bundle both."""
        da = _import_deploy_agent()
        pkgs = da.AGENT_EXTRA_PACKAGES["creative_agent"]
        assert "./creative_agent" in pkgs
        assert "./creative_eval" in pkgs
        assert "./agent_common" in pkgs

    def test_interactive_creative_bundles_full_graph(self):
        """interactive_creative imports creative_agent + creative_eval + agent_common."""
        da = _import_deploy_agent()
        pkgs = da.AGENT_EXTRA_PACKAGES["interactive_creative"]
        for dep in (
            "./interactive_creative",
            "./creative_agent",
            "./creative_eval",
            "./agent_common",
        ):
            assert dep in pkgs, f"{dep} missing from interactive_creative bundle"

    def test_trend_trawler_bundles_agent_common(self):
        da = _import_deploy_agent()
        pkgs = da.AGENT_EXTRA_PACKAGES["trend_trawler"]
        assert "./trend_trawler" in pkgs
        assert "./agent_common" in pkgs

    def test_root_package_listed_first(self):
        """The agent's own package should be the first bundled dir (root first)."""
        da = _import_deploy_agent()
        for name, pkgs in da.AGENT_EXTRA_PACKAGES.items():
            assert pkgs[0] == f"./{name}", f"{name}: root pkg not first ({pkgs})"

    def test_mapping_covers_every_deployable_agent(self):
        """The extra_packages map and deploy specs must cover the same agent set,
        so a new --agent value can't ship without a bundle definition."""
        da = _import_deploy_agent()
        assert set(da.AGENT_EXTRA_PACKAGES) == set(da.AGENT_NAMES)
        assert set(da.AGENT_DEPLOY_SPECS) == set(da.AGENT_NAMES)

    def test_interactive_creative_is_deployable(self):
        da = _import_deploy_agent()
        assert "interactive_creative" in da.AGENT_NAMES

    def test_all_bundled_dirs_exist_on_disk(self):
        """Every dir in every bundle must exist — the guard against a typo'd path."""
        da = _import_deploy_agent()
        for name, pkgs in da.AGENT_EXTRA_PACKAGES.items():
            for p in pkgs:
                abs_p = os.path.join(PROJECT_ROOT, p)
                assert os.path.isdir(abs_p), f"{name}: bundled dir missing: {p}"


class TestValidateExtraPackages:
    def test_passes_for_real_dirs(self):
        da = _import_deploy_agent()
        # Should not raise for a valid bundle.
        da.validate_extra_packages(da.AGENT_EXTRA_PACKAGES["trend_trawler"])

    def test_raises_for_missing_dir(self):
        da = _import_deploy_agent()
        with pytest.raises(FileNotFoundError):
            da.validate_extra_packages(["./trend_trawler", "./does_not_exist_pkg"])
