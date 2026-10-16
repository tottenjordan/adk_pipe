"""Tests for deployment utility functions (deploy_agent.py)."""

import importlib
import importlib.util
import logging
import os
import sys
import types
from unittest.mock import MagicMock

import dotenv
import pytest

from tests.repo_layout import REPO_ROOT, package_dir


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
    "GOOGLE_GENAI_USE_ENTERPRISE",
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
        # Verify the expected keys against .env.example (the source of the
        # deploy env), independent of deploy_agent.py's agentplatform.Client.
        env_example_path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
        assert os.path.exists(env_example_path), ".env.example not found"

        env_values = dotenv.dotenv_values(env_example_path)
        for key in EXPECTED_ENV_VAR_KEYS:
            assert key in env_values, f"Missing {key} in .env.example"

    def test_env_example_has_agent_engine_ids(self):
        """Verify .env.example has placeholder Agent Engine ID fields."""
        env_example_path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
        assert os.path.exists(env_example_path), ".env.example not found"

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
        assert os.path.exists(req_path), "requirements.txt not found"
        with open(req_path) as f:
            content = f.read()
        assert "google-adk" in content


# --- Agent Engine location resolution ---
# Replicate the deploy/test/integration client's location logic to avoid the
# agentplatform.Client() construction. Agent Engine is a *regional* resource,
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
        assert os.path.exists(env_example_path), ".env.example not found"
        env_values = dotenv.dotenv_values(env_example_path)
        assert env_values.get("GCP_REGION") == "us-central1"


# --- Part B: centralized extra_packages mapping ---
# deploy_agent.py is now importable without GCP creds (lazy agentplatform.Client via
# _get_client), so we assert on the REAL mapping/specs rather than a replica.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _import_deploy_agent():
    """Import deploy_agent.py, skipping if its (non-cred) deps are unavailable."""
    try:
        import deployment.deploy_agent as deploy_agent
    except ImportError as e:  # e.g. agentplatform/absl missing in a bare env
        pytest.skip(f"deploy_agent import unavailable: {e}")
    return deploy_agent


def _bundle_path(name: str) -> str:
    """The AGENT_EXTRA_PACKAGES entry for a package: its repo-relative dir."""
    return f"./{package_dir(name).relative_to(REPO_ROOT).as_posix()}"


class TestAgentExtraPackages:
    def test_creative_agent_bundles_sibling_deps(self):
        """creative_agent imports creative_eval + agent_common → must bundle both."""
        da = _import_deploy_agent()
        pkgs = da.AGENT_EXTRA_PACKAGES["creative_agent"]
        assert _bundle_path("creative_agent") in pkgs
        assert _bundle_path("creative_eval") in pkgs
        assert _bundle_path("agent_common") in pkgs

    def test_interactive_creative_bundles_full_graph(self):
        """interactive_creative imports creative_agent + creative_eval + agent_common."""
        da = _import_deploy_agent()
        pkgs = da.AGENT_EXTRA_PACKAGES["interactive_creative"]
        for dep in map(
            _bundle_path,
            ("interactive_creative", "creative_agent", "creative_eval", "agent_common"),
        ):
            assert dep in pkgs, f"{dep} missing from interactive_creative bundle"

    def test_trend_scout_bundles_agent_common(self):
        da = _import_deploy_agent()
        pkgs = da.AGENT_EXTRA_PACKAGES["trend_scout"]
        assert _bundle_path("trend_scout") in pkgs
        assert _bundle_path("agent_common") in pkgs

    def test_root_package_listed_first(self):
        """The agent's own package should be the first bundled dir (root first)."""
        da = _import_deploy_agent()
        for name, pkgs in da.AGENT_EXTRA_PACKAGES.items():
            assert pkgs[0] == _bundle_path(name), f"{name}: root pkg not first ({pkgs})"

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


# --- resolve_deploy_target: deploy the App, not the bare root_agent ---
# Agent Engine must receive the module's `App` when one is exported, otherwise its
# ResumabilityConfig (LongRunningFunctionTool checkpoints) and plugins are dropped.
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
            ("creative_agent", "app"),
        ],
    )
    def test_real_agent_modules_resolve_as_expected(self, name, expected_kind):
        da = _import_deploy_agent()
        module = importlib.import_module(da.AGENT_DEPLOY_SPECS[name]["module"])
        kind, target = da.resolve_deploy_target(module)
        assert kind == expected_kind
        assert target is module.app
        assert target.root_agent is module.root_agent
        # Resumability is per-agent: only the agents with LongRunningFunctionTool
        # checkpoints are resumable; creative_agent's App is non-resumable (it exists
        # to carry App-level plugins).
        resumable = target.resumability_config is not None and (
            target.resumability_config.is_resumable
        )
        assert resumable is (name != "creative_agent")

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

    @pytest.mark.parametrize("script", ["test_deployment", "integration_test"])
    def test_cli_agent_choices_include_every_deployable_agent(self, script):
        """Each script's --agent choices cover every deployable agent. Built
        in-process via build_parser() (no `--help` subprocess)."""
        da = _import_deploy_agent()
        module = _load_script(script)
        agent_actions = [
            a for a in module.build_parser()._actions if "--agent" in a.option_strings
        ]
        assert agent_actions, f"{script} has no --agent option"
        assert set(agent_actions[0].choices) == set(da.AGENT_DEPLOY_SPECS)


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

    def test_update_env_file_raises_when_the_write_fails(self, tmp_path, caplog):
        """A failed .env write must not look like success (it was logged at INFO
        and swallowed, so `engine_handoff apply` exited 0 with the old ID kept)."""
        da = _import_deploy_agent()
        missing_dir = tmp_path / "no-such-dir" / ".env"
        with caplog.at_level("INFO"), pytest.raises(OSError):
            da.update_env_file("trend_scout", "12345", str(missing_dir))
        assert any(
            r.levelname == "ERROR" and "SCOUT_AGENT_ENGINE_ID" in r.getMessage()
            for r in caplog.records
        )

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

    @staticmethod
    def _delete_client(display_name="creative-trend-agent-v1"):
        client = MagicMock(spec=["runtimes"])
        client.runtimes.get.return_value.api_resource.display_name = display_name
        op = client.runtimes.delete.return_value
        op.name = "operations/789"
        op.error = None
        return client

    @pytest.fixture
    def delete_env(self, monkeypatch):
        da = _import_deploy_agent()
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_NUMBER", "123")
        for name in da.AGENT_NAMES:
            monkeypatch.delenv(da.engine_env_key(name), raising=False)
        return da

    def test_delete_refuses_the_sessions_engine(self, monkeypatch, delete_env):
        da = delete_env
        client = self._delete_client("trend-trawler-sessions")
        monkeypatch.setattr(da, "_get_client", lambda: client)
        with pytest.raises(ValueError, match="trend-trawler-sessions"):
            da.delete("456")
        client.runtimes.delete.assert_not_called()

    def test_delete_refuses_an_engine_named_in_env(self, monkeypatch, delete_env):
        da = delete_env
        monkeypatch.setenv(
            "CREATIVE_AGENT_ENGINE_ID",
            "projects/123/locations/us-central1/reasoningEngines/456",
        )
        client = self._delete_client()
        monkeypatch.setattr(da, "_get_client", lambda: client)
        with pytest.raises(ValueError, match="CREATIVE_AGENT_ENGINE_ID"):
            da.delete("456")
        client.runtimes.delete.assert_not_called()

    def test_delete_protected_engine_with_override(self, monkeypatch, delete_env):
        da = delete_env
        monkeypatch.setenv("SCOUT_AGENT_ENGINE_ID", "456")
        client = self._delete_client("trend-trawler-sessions")
        monkeypatch.setattr(da, "_get_client", lambda: client)
        da.delete("456", allow_protected=True)
        client.runtimes.delete.assert_called_once()

    def test_delete_accepts_a_full_resource_name(self, monkeypatch, delete_env):
        da = delete_env
        client = self._delete_client()
        monkeypatch.setattr(da, "_get_client", lambda: client)
        da.delete("projects/123/locations/us-central1/reasoningEngines/456")
        expected = (
            f"projects/123/locations/{da.AGENT_ENGINE_LOCATION}/reasoningEngines/456"
        )
        client.runtimes.get.assert_called_once_with(name=expected)
        client.runtimes.delete.assert_called_once_with(name=expected, force=True)

    def test_delete_requires_the_project_number(self, monkeypatch, delete_env):
        da = delete_env
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_NUMBER")
        client = self._delete_client()
        monkeypatch.setattr(da, "_get_client", lambda: client)
        with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT_NUMBER"):
            da.delete("456")
        client.runtimes.delete.assert_not_called()

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


# --- deploy_agent(): the runtimes.create path (client, AdkApp, .env all mocked) ---
# Fake agent modules are injected into sys.modules so no heavy agent package is
# imported; resolve_deploy_target runs for real, so an App export must yield
# AdkApp(app=...) and a bare module AdkApp(agent=root_agent).
class TestDeployAgentCreate:
    RESOURCE = "projects/1/locations/us-central1/reasoningEngines/42"

    def _setup(self, monkeypatch, name, *, with_app):
        from google.adk.apps import App

        da = _import_deploy_agent()
        import agentplatform.frameworks

        root_agent = types.SimpleNamespace(description=f"{name} desc")
        module = types.SimpleNamespace(root_agent=root_agent)
        if with_app:
            module.app = MagicMock(spec=App)
        monkeypatch.setitem(sys.modules, da.AGENT_DEPLOY_SPECS[name]["module"], module)

        adk_app_cls = MagicMock()
        monkeypatch.setattr(agentplatform.frameworks, "AdkApp", adk_app_cls)
        client = MagicMock(spec=["runtimes"])
        client.runtimes.create.return_value.api_resource.name = self.RESOURCE
        # Record what the SDK would tar: create() runs from the staging root.
        self.create_cwd_files = None

        def _snapshot_cwd(**_kwargs):
            files = set()
            for dirpath, _, names in os.walk("."):
                files.update(os.path.join(dirpath, n) for n in names)
            self.create_cwd_files = files
            return client.runtimes.create.return_value

        client.runtimes.create.side_effect = _snapshot_cwd
        monkeypatch.setattr(da, "_get_client", lambda: client)
        update_env = MagicMock()  # never touch the real .env
        monkeypatch.setattr(da, "update_env_file", update_env)
        monkeypatch.setenv("GOOGLE_CLOUD_STORAGE_BUCKET", "my-bucket")
        return da, module, adk_app_cls, client, update_env

    @pytest.mark.parametrize(
        ("name", "with_app"),
        [
            ("trend_scout", True),
            ("interactive_creative", True),
            ("creative_agent", False),
        ],
    )
    def test_create_builds_adkapp_config_and_updates_env(
        self, monkeypatch, name, with_app
    ):
        da, module, adk_app_cls, client, update_env = self._setup(
            monkeypatch, name, with_app=with_app
        )
        cwd_before = os.getcwd()
        da.deploy_agent(name, "v9")
        assert os.getcwd() == cwd_before  # the staging chdir is undone

        # Resumable agents deploy their App (keeps ResumabilityConfig); others
        # deploy the bare root_agent.
        if with_app:
            adk_app_cls.assert_called_once_with(app=module.app)
        else:
            adk_app_cls.assert_called_once_with(agent=module.root_agent)

        client.runtimes.create.assert_called_once()
        kwargs = client.runtimes.create.call_args.kwargs
        assert kwargs["agent"] is adk_app_cls.return_value
        spec = da.AGENT_DEPLOY_SPECS[name]
        config = kwargs["config"]
        assert config["requirements"] == da.load_requirements(da.REQUIREMENTS_PATH)
        # Staged flat: each entry under its own folder name (= import name).
        assert config["extra_packages"] == [
            f"./{os.path.basename(p)}" for p in da.AGENT_EXTRA_PACKAGES[name]
        ]
        # The SDK tars from a filtered staging copy, never the repo checkout.
        files = self.create_cwd_files
        assert files, "create() saw an empty staging dir"
        assert f"./{name}/agent.py" in files
        assert not [f for f in files if "/.adk/" in f or "__pycache__" in f]
        assert config["staging_bucket"] == "gs://my-bucket"
        assert config["gcs_dir_name"] == f"adk-pipe/{spec['gcs_subdir']}/v9/staging"
        assert config["display_name"] == f"{spec['display_name']}-v9"
        assert config["description"] == f"{name} desc"
        assert config["env_vars"] == da.ENV_VAR_DICT
        assert "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY" not in config["env_vars"]

        update_env.assert_called_once_with(
            name=name, agent_engine_id=self.RESOURCE, env_file_path=da.ENV_FILE_PATH
        )

    def test_create_with_tracing_sets_telemetry_env_only(self, monkeypatch):
        da, module, adk_app_cls, client, _ = self._setup(
            monkeypatch, "creative_agent", with_app=False
        )
        da.deploy_agent("creative_agent", "v9", enable_tracing=True)
        env = client.runtimes.create.call_args.kwargs["config"]["env_vars"]
        assert env["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] == "true"
        # AdkApp's own enable_tracing stays unset: True would force prompt/response
        # content capture into the spans.
        adk_app_cls.assert_called_once_with(agent=module.root_agent)

    def test_create_failure_reraises_without_env_update(self, monkeypatch):
        da, _, _, client, update_env = self._setup(
            monkeypatch, "creative_agent", with_app=False
        )
        client.runtimes.create.side_effect = RuntimeError("quota")
        with pytest.raises(RuntimeError, match="quota"):
            da.deploy_agent("creative_agent", "v9")
        update_env.assert_not_called()

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_create_without_bucket_fails_up_front(self, monkeypatch, value):
        da, _, adk_app_cls, client, _ = self._setup(
            monkeypatch, "creative_agent", with_app=False
        )
        if value is None:
            monkeypatch.delenv("GOOGLE_CLOUD_STORAGE_BUCKET")
        else:
            monkeypatch.setenv("GOOGLE_CLOUD_STORAGE_BUCKET", value)
        # Was a staging bucket of "gs://None" after every agent was imported.
        with pytest.raises(ValueError, match="GOOGLE_CLOUD_STORAGE_BUCKET"):
            da.deploy_agent("creative_agent", "v9")
        adk_app_cls.assert_not_called()
        client.runtimes.create.assert_not_called()

    def test_create_returns_resource_name_and_prints_nothing_by_default(
        self, monkeypatch, capsys
    ):
        da, _, _, _, _ = self._setup(monkeypatch, "creative_agent", with_app=False)
        assert da.deploy_agent("creative_agent", "v9") == self.RESOURCE
        # Local behaviour stays as before: no machine-readable stdout line.
        assert "ENGINE_ID" not in capsys.readouterr().out

    def test_no_write_env_skips_env_update(self, monkeypatch):
        da, _, _, _, update_env = self._setup(
            monkeypatch, "creative_agent", with_app=False
        )
        da.deploy_agent("creative_agent", "v9", write_env=False)
        update_env.assert_not_called()

    def test_output_json_writes_record_and_prints_engine_id_line(
        self, monkeypatch, tmp_path, capsys
    ):
        import json

        da, _, _, _, _ = self._setup(monkeypatch, "trend_scout", with_app=True)
        out = tmp_path / "ids" / "trend_scout.json"
        da.deploy_agent(
            "trend_scout",
            "v9",
            enable_tracing=True,
            write_env=False,
            output_json=str(out),
        )
        assert json.loads(out.read_text()) == {
            "agent": "trend_scout",
            "resource_name": self.RESOURCE,
            "env_key": "SCOUT_AGENT_ENGINE_ID",
            "version": "v9",
            "display_name": "trend-scout-agent-v9",
            "enable_tracing": True,
        }
        assert (
            f"ENGINE_ID agent=trend_scout id={self.RESOURCE}"
            in capsys.readouterr().out.splitlines()
        )

    def test_failed_create_writes_no_output_json(self, monkeypatch, tmp_path):
        da, _, _, client, _ = self._setup(monkeypatch, "creative_agent", with_app=False)
        client.runtimes.create.side_effect = RuntimeError("quota")
        out = tmp_path / "creative_agent.json"
        with pytest.raises(RuntimeError):
            da.deploy_agent("creative_agent", "v9", output_json=str(out))
        assert not out.exists()


class TestDeployCliFlags:
    def test_write_env_defaults_on_and_output_json_off(self):
        da = _import_deploy_agent()
        assert da.FLAGS["write_env"].default is True
        assert da.FLAGS["output_json"].default is None

    def test_main_passes_machine_mode_flags(self, monkeypatch):
        da = _import_deploy_agent()
        calls = {}
        monkeypatch.setattr(da, "deploy_agent", lambda **kw: calls.update(kw))
        da.FLAGS.unparse_flags()
        da.FLAGS(
            [
                "deploy_agent.py",
                "--agent=creative_agent",
                "--version=v1",
                "--create",
                "--nowrite_env",
                "--output_json=/tmp/x.json",
            ]
        )
        try:
            da.main([])
        finally:
            da.FLAGS.unparse_flags()
        assert calls == {
            "name": "creative_agent",
            "version": "v1",
            "enable_tracing": False,
            "write_env": False,
            "output_json": "/tmp/x.json",
        }


class TestIntegrationEventAndSessionHelpers:
    @pytest.mark.parametrize(
        "resp",
        [
            {"sessions": [{"id": "a"}, {"id": "b"}]},
            [{"id": "a"}, {"id": "b"}],
            {"sessions": [types.SimpleNamespace(id="a"), {"id": "b"}]},
            types.SimpleNamespace(
                sessions=[types.SimpleNamespace(id="a"), types.SimpleNamespace(id="b")]
            ),
        ],
    )
    def test_session_ids_handles_wrapper_and_list_shapes(self, resp):
        it = _import_integration_test()
        assert it._session_ids(resp) == ["a", "b"]

    @pytest.mark.parametrize("resp", [{"sessions": None}, {}, None, []])
    def test_session_ids_empty(self, resp):
        it = _import_integration_test()
        assert it._session_ids(resp) == []

    @pytest.mark.parametrize("key", ["function_call", "functionCall"])
    def test_function_call_names_accepts_both_casings(self, key):
        it = _import_integration_test()
        event = {"content": {"parts": [{key: {"name": "review_research"}}]}}
        assert it._function_call_names(event) == ["review_research"]

    @pytest.mark.parametrize(
        "event",
        [
            {"content": {"parts": [{"text": "hi"}]}},
            {"content": {"parts": None}},
            {"content": None},
            {"author": "x"},
            "not-a-dict",
        ],
    )
    def test_function_call_names_empty(self, event):
        it = _import_integration_test()
        assert it._function_call_names(event) == []

    def test_event_texts_skips_blank_and_none(self):
        it = _import_integration_test()
        event = {"content": {"parts": [{"text": "  "}, {"text": None}, {"text": "ok"}]}}
        assert it._event_texts(event) == ["ok"]

    def test_has_text_output_passes_on_text(self):
        it = _import_integration_test()
        events = [{"content": {"parts": [{"text": "done"}]}}]
        assert it._check_text_output("creative_agent", events).passed

    def test_has_text_output_paused_interactive_passes(self):
        it = _import_integration_test()
        events = [
            {"content": {"parts": [{"function_call": {"name": "review_research"}}]}}
        ]
        result = it._check_text_output("interactive_creative", events)
        assert result.passed
        assert "review_research" in result.message

    def test_has_text_output_checkpoint_does_not_count_for_other_agents(self):
        it = _import_integration_test()
        events = [
            {"content": {"parts": [{"function_call": {"name": "review_research"}}]}}
        ]
        assert not it._check_text_output("creative_agent", events).passed

    def test_has_text_output_interactive_without_checkpoint_fails(self):
        it = _import_integration_test()
        events = [{"content": {"parts": [{"function_call": {"name": "memorize"}}]}}]
        assert not it._check_text_output("interactive_creative", events).passed


# --- test_deployment: tool-call logging handles 2.x snake_case stream events ---
def _load_script(name):
    """Load a deployment/ script from its file spec WITHOUT registering it in
    sys.modules. Its side effects (.env load, argv parsing, client) live in
    main(), so importing is inert."""
    _import_deploy_agent()
    path = os.path.join(PROJECT_ROOT, "deployment", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_{name}_isolated", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestPrettyPrintEvent:
    @pytest.mark.parametrize(
        ("call_key", "resp_key"),
        [("function_call", "function_response"), ("functionCall", "functionResponse")],
    )
    def test_logs_function_call_and_response_in_both_casings(
        self, caplog, call_key, resp_key
    ):
        td = _load_script("test_deployment")
        event = {
            "author": "root_agent",
            "content": {
                "parts": [
                    {call_key: {"name": "memorize", "args": {"key": "k"}}},
                    {resp_key: {"name": "memorize", "response": {"ok": True}}},
                ]
            },
        }
        with caplog.at_level(logging.INFO):
            td.pretty_print_event(event)
        assert "[root_agent]: Function call: memorize" in caplog.text
        assert "[root_agent]: Function response: memorize" in caplog.text

    def test_snake_case_part_with_null_fields_logs_function_call(self, caplog):
        """Defensive: a part with a null ``text`` alongside ``function_call`` still logs the call."""
        td = _load_script("test_deployment")
        part = {"text": None, "function_call": {"name": "memorize", "args": {}}}
        event = {"author": "a", "content": {"parts": [part]}}
        with caplog.at_level(logging.INFO):
            td.pretty_print_event(event)
        assert "[a]: Function call: memorize" in caplog.text
        assert "[a]: None" not in caplog.text

    def test_null_content_and_parts_do_not_raise(self):
        td = _load_script("test_deployment")
        td.pretty_print_event({"author": "a", "content": None})
        td.pretty_print_event({"author": "a", "content": {"parts": None}})


class TestEngineResourceName:
    """test_deployment names the missing env key instead of calling
    runtimes.get(name=None)."""

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_unset_engine_id_exits_naming_the_key(self, monkeypatch, value):
        td = _load_script("test_deployment")
        if value is None:
            monkeypatch.delenv("SCOUT_AGENT_ENGINE_ID", raising=False)
        else:
            monkeypatch.setenv("SCOUT_AGENT_ENGINE_ID", value)
        with pytest.raises(SystemExit, match="SCOUT_AGENT_ENGINE_ID"):
            td.engine_resource_name("trend_scout")

    def test_set_engine_id_is_returned(self, monkeypatch):
        td = _load_script("test_deployment")
        monkeypatch.setenv("CREATIVE_AGENT_ENGINE_ID", " projects/p/x/123 ")
        assert td.engine_resource_name("creative_agent") == "projects/p/x/123"


class TestBuildTestState:
    """test_deployment seeds the campaign fields as session state for the
    creative agents (their state init setdefaults them), not trend_scout."""

    ENV = {
        "BRAND": "PRS",
        "TARGET_PRODUCT": "SE CE24",
        "KEY_SELLING_POINT": "tone",
        "TARGET_AUDIENCE": "guitarists",
        "TARGET_SEARCH_TREND": "powerball",
    }

    @pytest.mark.parametrize("agent", ["creative_agent", "interactive_creative"])
    def test_creative_agents_get_campaign_state(self, monkeypatch, agent):
        for k, v in self.ENV.items():
            monkeypatch.setenv(k, v)
        td = _load_script("test_deployment")
        assert td.build_test_state(agent) == {
            "brand": "PRS",
            "target_product": "SE CE24",
            "key_selling_points": "tone",
            "target_audience": "guitarists",
            "target_search_trends": "powerball",
        }

    def test_trend_scout_gets_no_state(self, monkeypatch):
        for k, v in self.ENV.items():
            monkeypatch.setenv(k, v)
        td = _load_script("test_deployment")
        assert td.build_test_state("trend_scout") is None

    def test_unset_env_vars_are_omitted(self, monkeypatch):
        for k in self.ENV:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("BRAND", "PRS")
        td = _load_script("test_deployment")
        assert td.build_test_state("creative_agent") == {"brand": "PRS"}


# --- opt-in Agent Engine Cloud Trace (P4b §3) ---
class TestTelemetryEnv:
    FLAG = "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"

    def test_tracing_off_by_default(self):
        da = _import_deploy_agent()
        env = da.build_env_vars()
        assert self.FLAG not in env
        assert "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS" not in env

    def test_tracing_disables_span_content_capture(self):
        # ADK spans default to carrying full prompts/responses (llm_request/_response).
        da = _import_deploy_agent()
        env = da.build_env_vars(enable_tracing=True)
        assert env["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] == "false"

    def test_tracing_enabled_sets_flag_without_reserved_vars(self):
        da = _import_deploy_agent()
        env = da.build_env_vars(enable_tracing=True)
        assert env[self.FLAG] == "true"
        # Reserved by Agent Engine (FAILED_PRECONDITION if shipped).
        assert "GOOGLE_CLOUD_LOCATION" not in env
        assert "GOOGLE_CLOUD_PROJECT" not in env

    def test_returns_a_copy(self):
        da = _import_deploy_agent()
        env = da.build_env_vars(enable_tracing=True)
        env["MUTATED"] = "x"
        assert "MUTATED" not in da.ENV_VAR_DICT
        assert self.FLAG not in da.ENV_VAR_DICT


# --- integration_test smoke: each agent must produce its real outputs ---
_CAMPAIGN = {
    "brand": "PRS",
    "target_product": "SE CE24 Electric Guitar",
    "target_audience": "Gen Z guitarists",
    "key_selling_points": "Versatile tone",
    "target_search_trends": "Powerball",
}


class TestSmokeCreativeOutputs:
    """A creative_agent run that ends early (e.g. the root's empty turns before
    finalize) still has the campaign keys + some text, so the smoke check also
    asserts the deliverables: finalize ran, the eval report and research PDF
    were saved and at least one image was rendered."""

    DONE = {
        "finalize_done": True,
        "eval_report_gcs_uri": "gs://b/f/out/creative_eval_report.json",
        "research_report_gcs_uri": "gs://b/f/out/research_report.pdf",
        "generated_images": {
            "Concept A": {"gcs_uri": "gs://b/f/out/a.png", "attempts": 1},
        },
    }

    def test_complete_run_passes(self):
        it = _import_integration_test()
        result = it.check_agent_outputs("creative_agent", dict(self.DONE))
        assert result is not None and result.passed
        assert result.name == "smoke:creative_agent:outputs"

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("finalize_done", None),
            ("finalize_done", False),
            ("eval_report_gcs_uri", None),
            ("eval_report_gcs_uri", ""),
            ("research_report_gcs_uri", None),
            ("research_report_gcs_uri", "  "),
        ],
    )
    def test_missing_output_fails_and_names_it(self, key, value):
        it = _import_integration_test()
        state = {**self.DONE, key: value}
        if value is None:
            del state[key]
        result = it.check_agent_outputs("creative_agent", state)
        assert result is not None and result.failed
        assert key in result.message

    @pytest.mark.parametrize(
        "images",
        [
            None,
            {},
            [],
            {"Concept A": {}},
            {"Concept A": {"gcs_uri": ""}},
            {"Concept A": {"gcs_uri": None, "attempts": 2}},
            {"Concept A": "gs://b/a.png"},
        ],
    )
    def test_no_rendered_image_fails(self, images):
        it = _import_integration_test()
        state = {**self.DONE, "generated_images": images}
        if images is None:
            del state["generated_images"]
        result = it.check_agent_outputs("creative_agent", state)
        assert result is not None and result.failed
        assert "generated_images" in result.message

    def test_one_rendered_image_among_failed_renders_passes(self):
        it = _import_integration_test()
        state = {
            **self.DONE,
            "generated_images": {
                "Concept A": {"gcs_uri": ""},
                "Concept B": {"gcs_uri": "gs://b/f/out/b.png"},
            },
        }
        result = it.check_agent_outputs("creative_agent", state)
        assert result is not None and result.passed

    def test_failure_message_carries_the_recorded_issue(self):
        it = _import_integration_test()
        state = {
            **self.DONE,
            "eval_report_gcs_uri": "",
            "eval_report_gcs_uri__issues": "eval report: 403 Forbidden",
        }
        result = it.check_agent_outputs("creative_agent", state)
        assert result is not None and result.failed
        assert "403 Forbidden" in result.message


class TestSmokeAgentOutputs:
    """trend_scout must select trends; interactive_creative (paused at
    checkpoint 1, after combined_research_pipeline) must have the brief and the
    research PDF."""

    @pytest.mark.parametrize("value", [None, "", "   ", [], {}])
    def test_trend_scout_without_selected_trends_fails(self, value):
        it = _import_integration_test()
        state = {"selected_gtrends": value}
        if value is None:
            state = {}
        result = it.check_agent_outputs("trend_scout", state)
        assert result is not None and result.failed
        assert result.name == "smoke:trend_scout:outputs"
        assert "selected_gtrends" in result.message

    def test_trend_scout_with_selected_trends_passes(self):
        it = _import_integration_test()
        state = {"selected_gtrends": "1. Powerball: jackpot fever ..."}
        result = it.check_agent_outputs("trend_scout", state)
        assert result is not None and result.passed

    INTERACTIVE = {
        "creative_brief": {"proposition": "Play every genre"},
        "research_report_gcs_uri": "gs://b/f/out/research_report.pdf",
    }

    def test_interactive_checkpoint_one_outputs_pass(self):
        it = _import_integration_test()
        result = it.check_agent_outputs("interactive_creative", dict(self.INTERACTIVE))
        assert result is not None and result.passed

    @pytest.mark.parametrize("key", ["creative_brief", "research_report_gcs_uri"])
    @pytest.mark.parametrize("value", [None, "", {}])
    def test_interactive_missing_output_fails(self, key, value):
        it = _import_integration_test()
        state = {**self.INTERACTIVE, key: value}
        if value is None:
            del state[key]
        result = it.check_agent_outputs("interactive_creative", state)
        assert result is not None and result.failed
        assert key in result.message

    def test_every_deployable_agent_has_output_keys(self):
        it = _import_integration_test()
        assert set(it.AGENT_OUTPUT_KEYS) == set(it.AGENT_ENV_KEYS)


class TestSmokeStateKeys:
    """The campaign keys are pre-seeded "" by every agent's state init, so mere
    presence proves nothing: each must hold a value."""

    @pytest.mark.parametrize("agent", ["trend_scout", "creative_agent"])
    def test_real_values_pass(self, agent):
        it = _import_integration_test()
        result = it.check_state_keys(agent, dict(_CAMPAIGN))
        assert result.passed
        assert result.name == f"smoke:{agent}:state_keys"

    @pytest.mark.parametrize(
        "agent", ["trend_scout", "creative_agent", "interactive_creative"]
    )
    def test_seeded_empty_strings_fail(self, agent):
        it = _import_integration_test()
        state = dict.fromkeys(_CAMPAIGN, "")
        result = it.check_state_keys(agent, state)
        assert result.failed
        assert "brand" in result.message

    @pytest.mark.parametrize("value", ["", "  ", None, [], {}])
    def test_one_empty_key_fails_and_names_it(self, value):
        it = _import_integration_test()
        state = {**_CAMPAIGN, "target_product": value}
        result = it.check_state_keys("creative_agent", state)
        assert result.failed
        assert "target_product" in result.message
        assert "brand" not in result.message.split("Present")[0]

    def test_absent_key_fails(self):
        it = _import_integration_test()
        state = {k: v for k, v in _CAMPAIGN.items() if k != "brand"}
        result = it.check_state_keys("trend_scout", state)
        assert result.failed
        assert "brand" in result.message


class TestSmokeRunEvents:
    def test_zero_events_fails(self):
        it = _import_integration_test()
        result = it.check_run_events("trend_scout", [], duration_s=1.0)
        assert result.failed
        assert result.name == "smoke:trend_scout:run"
        assert "No events" in result.message

    def test_events_pass(self):
        it = _import_integration_test()
        result = it.check_run_events("trend_scout", [{"author": "x"}], duration_s=1.0)
        assert result.passed
        assert result.duration_s == 1.0
        assert "1 events" in result.message


class TestDeployAgentRuntimeDeps:
    def test_import_does_not_pull_in_pandas(self):
        # Cloud Build deploys from `uv sync --no-dev`; pandas is dev-only.
        import subprocess

        code = (
            "import sys; import deployment.deploy_agent; "
            "sys.exit(1 if 'pandas' in sys.modules else 0)"
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "GOOGLE_CLOUD_PROJECT": "test-project"},
        )
        assert r.returncode == 0, r.stderr or "deploy_agent imported pandas"

    def test_default_version_is_utc_minute_stamp(self):
        import re

        da = _import_deploy_agent()
        assert re.fullmatch(r"\d{4}_\d{2}_\d{2}_\d{2}_\d{2}", da.default_version())


class TestHeadlessRun:
    """headless_run.py exits non-zero when the run produced nothing (it used to
    print a generate_image tool-call count, always 0 now, and exit 0)."""

    @pytest.mark.slow
    def test_empty_run_fails(self, monkeypatch, capsys):
        hr = _load_script("headless_run")

        class _NoopRunner:
            def __init__(self, **_kwargs):
                pass

            async def run_async(self, **_kwargs):
                return
                yield  # an async generator that yields no events

        monkeypatch.setattr(hr, "Runner", _NoopRunner)
        import asyncio

        assert asyncio.run(hr.main()) == 1
        assert "FAIL:" in capsys.readouterr().out
