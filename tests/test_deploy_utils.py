"""Tests for deployment utility functions (deploy_agent.py)."""

import importlib
import os
import re
import subprocess
import sys
import types
from unittest.mock import MagicMock

import dotenv
import pytest


# --- update_env_file (the real function; _import_deploy_agent is defined below) ---
class TestUpdateEnvFile:
    def test_writes_scout_key(self, tmp_path):
        da = _import_deploy_agent()
        env_file = tmp_path / ".env"
        env_file.write_text("")
        da.update_env_file("trend_scout", "12345", str(env_file))
        assert dotenv.dotenv_values(env_file) == {"SCOUT_AGENT_ENGINE_ID": "12345"}

    def test_overwrites_existing_value(self, tmp_path):
        da = _import_deploy_agent()
        env_file = tmp_path / ".env"
        env_file.write_text('CREATIVE_AGENT_ENGINE_ID="old_id"\n')
        da.update_env_file("creative_agent", "new_id", str(env_file))
        assert dotenv.dotenv_values(env_file) == {"CREATIVE_AGENT_ENGINE_ID": "new_id"}

    def test_preserves_other_keys(self, tmp_path):
        da = _import_deploy_agent()
        env_file = tmp_path / ".env"
        env_file.write_text('SOME_OTHER_KEY="keep_me"\n')
        da.update_env_file("trend_scout", "12345", str(env_file))
        values = dotenv.dotenv_values(env_file)
        assert values["SOME_OTHER_KEY"] == "keep_me"
        assert values["SCOUT_AGENT_ENGINE_ID"] == "12345"


# --- ENV_VAR_DICT keys ---
EXPECTED_ENV_VAR_KEYS = [
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT_NUMBER",
    "GOOGLE_CLOUD_STORAGE_BUCKET",
    "BQ_PROJECT_ID",
    "BQ_DATASET_ID",
    "BQ_TABLE_TARGETS",
    "BQ_TABLE_CREATIVES",
    "BQ_TABLE_EVALS",
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
        assert "SCOUT_AGENT_ENGINE_ID" in env_values

    def test_requirements_file_exists(self):
        """Verify requirements.txt exists (referenced by deploy script)."""
        req_path = os.path.join(os.path.dirname(__file__), "..", "requirements.txt")
        assert os.path.exists(req_path), "requirements.txt not found at project root"

    def test_requirements_includes_adk(self):
        """Verify requirements.txt includes google-adk."""
        req_path = os.path.join(os.path.dirname(__file__), "..", "requirements.txt")
        if not os.path.exists(req_path):
            pytest.skip("requirements.txt not found")
        with open(req_path) as f:
            content = f.read()
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

    def test_trend_scout_bundles_agent_common(self):
        da = _import_deploy_agent()
        pkgs = da.AGENT_EXTRA_PACKAGES["trend_scout"]
        assert "./trend_scout" in pkgs
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
        da.validate_extra_packages(da.AGENT_EXTRA_PACKAGES["trend_scout"])

    def test_raises_for_missing_dir(self):
        da = _import_deploy_agent()
        with pytest.raises(FileNotFoundError):
            da.validate_extra_packages(["./trend_scout", "./does_not_exist_pkg"])


# --- resolve_deploy_target: deploy the resumable App, not the bare root_agent ---
# Agent Engine must receive the module's `App` (with ResumabilityConfig) when one
# is exported, otherwise LongRunningFunctionTool review checkpoints can't pause.
class TestResolveDeployTarget:
    def test_prefers_resumable_app_when_module_exports_one(self):
        from google.adk.apps import App

        da = _import_deploy_agent()
        app = MagicMock(spec=App)
        mod = types.SimpleNamespace(root_agent="agent", app=app)
        assert da.resolve_deploy_target(mod) == ("app", app)

    def test_non_app_attribute_falls_back_to_root_agent(self):
        """An unrelated `app` attr (e.g. a FastAPI app) must not be deployed."""
        da = _import_deploy_agent()
        mod = types.SimpleNamespace(root_agent="agent", app="not-an-App")
        assert da.resolve_deploy_target(mod) == ("agent", "agent")

    def test_falls_back_to_root_agent(self):
        da = _import_deploy_agent()
        mod = types.SimpleNamespace(root_agent="agent")
        assert da.resolve_deploy_target(mod) == ("agent", "agent")

    @pytest.mark.parametrize(
        ("name", "expected_kind"),
        [
            ("trend_scout", "app"),
            ("interactive_creative", "app"),
            ("creative_agent", "agent"),
        ],
    )
    def test_real_agent_modules_resolve_as_expected(self, name, expected_kind):
        da = _import_deploy_agent()
        module = importlib.import_module(da.AGENT_DEPLOY_SPECS[name]["module"])
        kind, target = da.resolve_deploy_target(module)
        assert kind == expected_kind
        if kind == "app":
            assert target is module.app
            assert target.resumability_config.is_resumable is True
            assert target.root_agent is module.root_agent
        else:
            assert target is module.root_agent

    def test_every_deploy_spec_is_covered(self):
        da = _import_deploy_agent()
        assert set(da.AGENT_DEPLOY_SPECS) == {
            "trend_scout",
            "interactive_creative",
            "creative_agent",
        }


# --- test/integration scripts derive --agent choices from AGENT_DEPLOY_SPECS ---
class TestScriptAgentChoices:
    def test_integration_env_keys_derived_from_specs(self):
        da = _import_deploy_agent()
        import deployment.integration_test as it

        assert set(it.AGENT_ENV_KEYS) == set(da.AGENT_DEPLOY_SPECS)
        for name in da.AGENT_DEPLOY_SPECS:
            assert it.AGENT_ENV_KEYS[name] == da.engine_env_key(name)

    def test_integration_expected_state_keys_cover_every_agent(self):
        da = _import_deploy_agent()
        import deployment.integration_test as it

        assert set(it.EXPECTED_STATE_KEYS) == set(da.AGENT_DEPLOY_SPECS)

    @pytest.mark.parametrize(
        "script", ["deployment/test_deployment.py", "deployment/integration_test.py"]
    )
    def test_cli_agent_choices_include_every_deployable_agent(self, script):
        da = _import_deploy_agent()
        result = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, script), "--help"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=120,
            check=True,
        )
        # Match inside argparse's `{a,b,c}` choices set, not a bare substring
        # (which could hit the epilog/help text).
        choice_sets = re.findall(r"\{([^}]*)\}", result.stdout)
        agent_sets = [
            set(c.split(",")) for c in choice_sets if "creative_agent" in c.split(",")
        ]
        assert agent_sets, f"{script} --help has no --agent choices set"
        for name in da.AGENT_DEPLOY_SPECS:
            assert re.search(r"\{[^}]*" + re.escape(name) + r"[^}]*\}", result.stdout)
            assert name in agent_sets[0], f"{script} --agent missing {name}"


# --- engine_env_key: one source of truth for `<PREFIX>_AGENT_ENGINE_ID` ---
class TestEngineEnvKey:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("trend_scout", "SCOUT_AGENT_ENGINE_ID"),
            ("creative_agent", "CREATIVE_AGENT_ENGINE_ID"),
            ("interactive_creative", "INTERACTIVE_AGENT_ENGINE_ID"),
        ],
    )
    def test_key_format(self, name, expected):
        da = _import_deploy_agent()
        assert da.engine_env_key(name) == expected

    def test_update_env_file_writes_engine_env_key(self, tmp_path):
        da = _import_deploy_agent()
        env_file = tmp_path / ".env"
        env_file.write_text("")
        da.update_env_file("interactive_creative", "abc123", str(env_file))
        assert dotenv.dotenv_values(env_file) == {
            "INTERACTIVE_AGENT_ENGINE_ID": "abc123"
        }


# --- integration_test: an undeployed agent (unset engine ID) is a SKIP ---
def _import_integration_test():
    _import_deploy_agent()
    import deployment.integration_test as it

    return it


def _unset_all_engine_ids(monkeypatch, it, keep=()):
    for name, key in it.AGENT_ENV_KEYS.items():
        if name not in keep:
            monkeypatch.delenv(key, raising=False)


class TestIntegrationMissingEngineIdSkips:
    def test_health_missing_id_is_skip_not_failure(self, monkeypatch):
        it = _import_integration_test()
        _unset_all_engine_ids(monkeypatch, it)
        results = it.check_health(client=MagicMock())
        assert len(results) == len(it.AGENT_ENV_KEYS)
        assert all(r.skipped and not r.failed for r in results)

    def test_health_mixes_pass_and_skip(self, monkeypatch):
        it = _import_integration_test()
        _unset_all_engine_ids(monkeypatch, it, keep=("trend_scout",))
        monkeypatch.setenv(it.AGENT_ENV_KEYS["trend_scout"], "projects/p/x/1")
        client = MagicMock()
        results = {r.name: r for r in it.check_health(client)}
        assert results["health:trend_scout"].passed
        assert results["health:interactive_creative"].skipped
        assert it.print_results(list(results.values())) is True

    @pytest.mark.parametrize("check", ["check_session", "check_smoke"])
    def test_session_and_smoke_missing_id_is_skip(self, monkeypatch, check):
        import asyncio

        it = _import_integration_test()
        _unset_all_engine_ids(monkeypatch, it)
        results = asyncio.run(getattr(it, check)(MagicMock(), "interactive_creative"))
        assert len(results) == 1
        assert results[0].skipped and not results[0].failed

    def test_print_results_success_with_only_skips_and_passes(self, capsys):
        it = _import_integration_test()
        results = [
            it.TestResult(name="a", passed=True, message="ok"),
            it.TestResult(name="b", passed=False, message="unset", skipped=True),
        ]
        assert it.print_results(results) is True
        out = capsys.readouterr().out
        assert "[SKIP] b" in out
        assert "1 passed, 0 failed, 1 skipped, 2 total" in out

    def test_print_results_fails_on_real_failure(self):
        it = _import_integration_test()
        results = [
            it.TestResult(name="a", passed=False, message="boom"),
            it.TestResult(name="b", passed=False, message="unset", skipped=True),
        ]
        assert it.print_results(results) is False

    def test_main_exits_zero_when_only_skips(self, monkeypatch):
        it = _import_integration_test()
        _unset_all_engine_ids(monkeypatch, it)
        monkeypatch.setattr(it, "get_client", MagicMock)
        monkeypatch.setattr(sys, "argv", ["integration_test.py", "--check", "all"])
        with pytest.raises(SystemExit) as exc:
            it.main()
        assert exc.value.code == 0


class TestRuntimesApi:
    def test_list_agents_uses_runtimes_and_handles_empty_generator(
        self, monkeypatch, caplog
    ):
        da = _import_deploy_agent()
        client = MagicMock(spec=["runtimes"])
        client.runtimes.list.return_value = iter([])
        monkeypatch.setattr(da, "_get_client", lambda: client)
        with caplog.at_level("INFO"):
            da.list_agents()
        # A generator is always truthy, so the old `if not remote_agents` never
        # reported an empty project.
        assert "No agents found." in caplog.text

    def test_delete_uses_runtimes_delete_with_force(self, monkeypatch, caplog):
        da = _import_deploy_agent()
        client = MagicMock(spec=["runtimes"])
        monkeypatch.setattr(da, "_get_client", lambda: client)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_NUMBER", "123")
        op = client.runtimes.delete.return_value
        op.name = "operations/789"  # MagicMock(name=...) sets the repr, not .name
        op.error = None
        with caplog.at_level("INFO"):
            da.delete("456")
        client.runtimes.delete.assert_called_once_with(
            name=f"projects/123/locations/{da.AGENT_ENGINE_LOCATION}/reasoningEngines/456",
            force=True,
        )
        # runtimes.delete returns an unpolled operation — don't claim success.
        assert "Delete requested for 456 (operation operations/789)" in caplog.text
        assert "Successfully deleted" not in caplog.text

    def test_delete_warns_on_operation_error(self, monkeypatch, caplog):
        da = _import_deploy_agent()
        client = MagicMock(spec=["runtimes"])
        op = client.runtimes.delete.return_value
        op.name = "operations/789"
        op.error = {"code": 9, "message": "boom"}
        monkeypatch.setattr(da, "_get_client", lambda: client)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_NUMBER", "123")
        with caplog.at_level("INFO"):
            da.delete("456")
        assert any(
            r.levelname == "WARNING" and "boom" in r.getMessage()
            for r in caplog.records
        )

    def test_client_is_agentplatform(self, monkeypatch):
        da = _import_deploy_agent()
        ctor = MagicMock()
        monkeypatch.setattr(da.agentplatform, "Client", ctor)
        monkeypatch.setattr(da, "_client", None)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-x")  # read at call time
        first = da._get_client()
        assert ctor.call_args.kwargs["location"] == da.AGENT_ENGINE_LOCATION
        assert ctor.call_args.kwargs["project"] == "proj-x"
        # Cached: a second call must not construct another client.
        assert da._get_client() is first
        ctor.assert_called_once()

    def test_list_agents_logs_runtime_fields(self, monkeypatch, caplog):
        da = _import_deploy_agent()
        runtime = MagicMock()
        runtime.api_resource.name = (
            "projects/1/locations/us-central1/reasoningEngines/42"
        )
        runtime.api_resource.display_name = "trend-scout-v9"
        runtime.api_resource.create_time = "2026-09-29T00:00:00Z"
        runtime.api_resource.update_time = "2026-09-29T01:00:00Z"
        runtime.api_resource.description = "scout"
        client = MagicMock(spec=["runtimes"])
        client.runtimes.list.return_value = iter([runtime])
        monkeypatch.setattr(da, "_get_client", lambda: client)
        with caplog.at_level("INFO"):
            da.list_agents()
        assert "projects/1/locations/us-central1/reasoningEngines/42" in caplog.text
        assert "trend-scout-v9" in caplog.text
        assert "No agents found." not in caplog.text
