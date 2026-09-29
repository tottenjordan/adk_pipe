"""Shared lazy Google Cloud client getters for the agent packages.

The google-cloud-storage / google-cloud-bigquery imports happen inside the
functions, so importing ``agent_common`` stays light and a module that never
touches GCS/BigQuery never loads those SDKs. Each getter builds a fresh client
per call; a caller that wants reuse wraps it (e.g. ``functools.cache``).

Agent modules bind these to their historical private names
(``_get_gcs_client`` / ``_get_bigquery_client``) so the existing call sites and
test monkeypatch points keep working.
"""

from typing import TYPE_CHECKING

from agent_common.config import BaseAgentConfiguration

if TYPE_CHECKING:
    from google.cloud import bigquery, storage


def get_gcs_client() -> "storage.Client":
    """Build a Cloud Storage client for the configured GCP project."""
    from google.cloud import storage

    return storage.Client(project=BaseAgentConfiguration.PROJECT_ID)


def get_bigquery_client() -> "bigquery.Client":
    """Build a BigQuery client for the configured BigQuery project."""
    from google.cloud import bigquery

    return bigquery.Client(project=BaseAgentConfiguration.BQ_PROJECT_ID)
