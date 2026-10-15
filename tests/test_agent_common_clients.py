"""Tests for agent_common.clients (shared lazy GCS/BigQuery client getters)."""

import threading

import pytest

import agent_common.clients as clients
from agent_common.config import BaseAgentConfiguration


class _FakeClient:
    def __init__(self, project=None):
        self.project = project


def test_gcs_client_uses_project_id(monkeypatch):
    from google.cloud import storage

    monkeypatch.setattr(storage, "Client", _FakeClient)
    monkeypatch.setattr(BaseAgentConfiguration, "PROJECT_ID", "proj-x")
    client = clients.get_gcs_client()
    assert isinstance(client, _FakeClient)
    assert client.project == "proj-x"


def test_bigquery_client_uses_bq_project_id(monkeypatch):
    from google.cloud import bigquery

    monkeypatch.setattr(bigquery, "Client", _FakeClient)
    monkeypatch.setattr(BaseAgentConfiguration, "BQ_PROJECT_ID", "bq-proj")
    client = clients.get_bigquery_client()
    assert isinstance(client, _FakeClient)
    assert client.project == "bq-proj"


def test_agent_modules_bind_the_shared_getters():
    from creative_agent import bq_tools, gcs_tools
    from trend_scout import tools as scout_tools

    assert scout_tools._get_gcs_client is clients.get_gcs_client
    assert scout_tools._get_bigquery_client is clients.get_bigquery_client
    assert bq_tools._get_bigquery_client is clients.get_bigquery_client
    # creative_agent caches its GCS client around the shared getter.
    assert gcs_tools._get_gcs_client.__wrapped__ is clients.get_gcs_client


# --- per-thread BigQuery client reuse ---


@pytest.fixture
def fake_bigquery(monkeypatch):
    from google.cloud import bigquery

    built = []

    class FakeClient:
        def __init__(self, project=None):
            self.project = project
            built.append(self)

    monkeypatch.setattr(bigquery, "Client", FakeClient)
    return built


def test_bigquery_client_is_reused_within_a_thread(fake_bigquery):
    from agent_common import clients

    first = clients.get_bigquery_client()
    assert clients.get_bigquery_client() is first
    assert len(fake_bigquery) == 1


def test_each_thread_gets_its_own_client(fake_bigquery):
    from agent_common import clients

    main = clients.get_bigquery_client()
    seen = []

    def worker():
        seen.append(clients.get_bigquery_client())
        seen.append(clients.get_bigquery_client())

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen[0] is seen[1]
    assert seen[0] is not main
    assert len(fake_bigquery) == 2


def test_reset_drops_the_cached_client(fake_bigquery):
    from agent_common import clients

    first = clients.get_bigquery_client()
    clients.reset_bigquery_client_cache()
    assert clients.get_bigquery_client() is not first


def test_bq_project_change_builds_a_new_client(fake_bigquery, monkeypatch):
    """N-3 M23: the cache is keyed by project, so a changed BQ_PROJECT_ID
    never reuses a client bound to the old project."""
    from agent_common import clients

    monkeypatch.setattr(BaseAgentConfiguration, "BQ_PROJECT_ID", "proj-a")
    a = clients.get_bigquery_client()
    assert clients.get_bigquery_client() is a
    monkeypatch.setattr(BaseAgentConfiguration, "BQ_PROJECT_ID", "proj-b")
    b = clients.get_bigquery_client()
    assert b is not a
    assert (a.project, b.project) == ("proj-a", "proj-b")
    assert clients.get_bigquery_client() is b


def test_importing_agents_builds_no_bigquery_client():
    """Agent Engine pickles the agents at deploy: no client at import time.

    Runs in a fresh interpreter so no other test has imported the agents yet
    (and no module is reloaded under the rest of the suite)."""
    import os
    import subprocess
    import sys

    code = (
        "from google.cloud import bigquery\n"
        "def boom(*a, **k):\n"
        "    raise SystemExit('bigquery.Client built at import')\n"
        "bigquery.Client = boom\n"
        "import creative_agent.agent, interactive_creative.agent, trend_scout.agent\n"
        "import creative_agent.bq_tools, creative_agent.brand_history\n"
        "import creative_agent.rating_signals, trend_scout.digest, trend_scout.tools\n"
    )
    env = {**os.environ, "GOOGLE_CLOUD_PROJECT": "test-project"}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr[-2000:]
