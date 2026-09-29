"""Tests for `deployment/create_session_engine.py` (no GCP calls).

The script takes project/region from CLI args or the environment (no hardcoded
project), and uses the `vertexai.Client().agent_engines` API: reuse an engine
with the same display name, else create a sessions-only (agent-less) engine.
"""

import types
from unittest.mock import MagicMock

import pytest

from deployment import create_session_engine as cse


def _engine(name, display_name):
    return types.SimpleNamespace(
        api_resource=types.SimpleNamespace(name=name, display_name=display_name)
    )


class TestParseArgs:
    def test_project_and_region_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-proj")
        monkeypatch.setenv("GCP_REGION", "europe-west4")
        args = cse.parse_args([])
        assert (args.project, args.region) == ("env-proj", "europe-west4")
        assert args.display_name == cse.DEFAULT_DISPLAY_NAME

    def test_region_defaults_to_us_central1_not_model_location(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-proj")
        monkeypatch.delenv("GCP_REGION", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
        assert cse.parse_args([]).region == "us-central1"

    def test_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-proj")
        args = cse.parse_args(["--project", "cli-proj", "--region", "asia-east1"])
        assert (args.project, args.region) == ("cli-proj", "asia-east1")

    def test_missing_project_exits(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        with pytest.raises(SystemExit):
            cse.parse_args([])


class TestCreateOrReuse:
    def test_reuses_existing_engine_by_display_name(self):
        client = MagicMock()
        client.agent_engines.list.return_value = [
            _engine("projects/p/locations/r/reasoningEngines/1", "other"),
            _engine("projects/p/locations/r/reasoningEngines/2", "sessions"),
        ]
        name, created = cse.create_or_reuse(client, "sessions")
        assert (name, created) == ("projects/p/locations/r/reasoningEngines/2", False)
        client.agent_engines.create.assert_not_called()

    def test_creates_agentless_engine_when_absent(self):
        client = MagicMock()
        client.agent_engines.list.return_value = []
        client.agent_engines.create.return_value = _engine(
            "projects/p/locations/r/reasoningEngines/9", "sessions"
        )
        name, created = cse.create_or_reuse(client, "sessions")
        assert (name, created) == ("projects/p/locations/r/reasoningEngines/9", True)
        kwargs = client.agent_engines.create.call_args.kwargs
        assert "agent" not in kwargs and "agent_engine" not in kwargs
        assert kwargs["config"]["display_name"] == "sessions"

    def test_create_without_resource_raises(self):
        client = MagicMock()
        client.agent_engines.list.return_value = []
        client.agent_engines.create.return_value = types.SimpleNamespace(
            api_resource=None
        )
        with pytest.raises(RuntimeError, match="returned no resource"):
            cse.create_or_reuse(client, "sessions")


def test_main_builds_client_from_args(monkeypatch, capsys):
    client = MagicMock()
    client.agent_engines.list.return_value = [
        _engine("projects/p/locations/r/reasoningEngines/2", "trend-trawler-sessions")
    ]
    ctor = MagicMock(return_value=client)
    monkeypatch.setattr(cse.vertexai, "Client", ctor)
    monkeypatch.setattr(cse.dotenv, "load_dotenv", lambda **_: None)
    cse.main(["--project", "cli-proj", "--region", "us-east1"])
    ctor.assert_called_once_with(project="cli-proj", location="us-east1")
    out = capsys.readouterr().out
    assert (
        "SESSION_SERVICE_URI=agentengine://projects/p/locations/r/reasoningEngines/2"
        in out
    )
