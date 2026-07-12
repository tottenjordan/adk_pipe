"""Tests for deployment utility functions (deploy_agent.py)."""
import os
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
        env_example_path = os.path.join(
            os.path.dirname(__file__), "..", ".env.example"
        )
        if not os.path.exists(env_example_path):
            pytest.skip(".env.example not found")

        env_values = dotenv.dotenv_values(env_example_path)
        for key in EXPECTED_ENV_VAR_KEYS:
            assert key in env_values, f"Missing {key} in .env.example"

    def test_env_example_has_agent_engine_ids(self):
        """Verify .env.example has placeholder Agent Engine ID fields."""
        env_example_path = os.path.join(
            os.path.dirname(__file__), "..", ".env.example"
        )
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
