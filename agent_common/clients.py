"""Shared lazy Google Cloud client getters for the agent packages.

The google-cloud-storage / google-cloud-bigquery imports happen inside the
functions, so importing ``agent_common`` stays light and a module that never
touches GCS/BigQuery never loads those SDKs. Nothing is built at import time
(Agent Engine pickles the agents at deploy, so a client must only ever be
created inside a running process).

BigQuery client reuse: Google recommends reusing one client for many requests
rather than building one per request, because each new client re-authenticates
and opens fresh connections
(https://docs.cloud.google.com/apis/docs/client-libraries-best-practices).
``get_bigquery_client`` therefore caches its client **per thread**: the client
sends HTTP through a ``requests.Session`` (google-auth ``AuthorizedSession``),
and requests has never documented a thread-safety contract for ``Session``
(https://github.com/psf/requests/issues/2766, closed without one), while the
agents call BigQuery from ``asyncio.to_thread`` workers concurrently. A
``threading.local`` keeps one client per worker thread — reused across calls,
since the default executor's threads are long-lived — without sharing a
Session between threads. ``reset_bigquery_client_cache`` drops the calling
thread's client (tests).

``get_gcs_client`` still builds a fresh client per call; a caller that wants
reuse wraps it (``creative_agent.gcs_tools`` uses ``functools.cache``).

Agent modules bind these to their historical private names
(``_get_gcs_client`` / ``_get_bigquery_client``) so the existing call sites and
test monkeypatch points keep working.
"""

import threading
from typing import TYPE_CHECKING

from agent_common import config as _cfg

if TYPE_CHECKING:
    from google.cloud import bigquery, storage

_bigquery_local = threading.local()


def get_gcs_client() -> "storage.Client":
    """Build a Cloud Storage client for the configured GCP project."""
    from google.cloud import storage

    return storage.Client(project=_cfg.BaseAgentConfiguration.PROJECT_ID)


def get_bigquery_client() -> "bigquery.Client":
    """Return this thread's BigQuery client for the configured project.

    Built on first use in each thread and reused afterwards. A change of
    ``BQ_PROJECT_ID`` (tests) builds a new client.
    """
    from google.cloud import bigquery

    project = _cfg.BaseAgentConfiguration.BQ_PROJECT_ID
    cached = getattr(_bigquery_local, "client", None)
    if cached is None or getattr(_bigquery_local, "project", None) != project:
        cached = bigquery.Client(project=project)
        _bigquery_local.client = cached
        _bigquery_local.project = project
    return cached


def reset_bigquery_client_cache() -> None:
    """Forget the calling thread's cached BigQuery client."""
    _bigquery_local.client = None
    _bigquery_local.project = None
