"""Tests for agent_common.clients (shared lazy GCS/BigQuery client getters)."""

import agent_common.clients as clients
from agent_common.config import BaseAgentConfiguration


class _FakeClient:
    def __init__(self, project=None):
        self.project = project


def test_sdk_imports_are_lazy():
    # The SDK modules are imported inside the getters, not at module level.
    assert not hasattr(clients, "storage")
    assert not hasattr(clients, "bigquery")


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
