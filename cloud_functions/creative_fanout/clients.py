"""GCP clients for the CRF functions.

One BigQuery client and one Pub/Sub publisher per instance, built on first
use (so importing needs no credentials) and reused by every invocation:
functions-framework serves requests on many threads (the orchestrator runs
at concurrency 100), so creation is guarded by a lock, and a per-invocation
client would leak its HTTP/gRPC transport (they were never closed). The Agent
Runtime client is NOT cached: it is bound to one event loop (see
`new_vertex_client`).
"""

import logging
import threading

import agentplatform
from google.cloud import bigquery, pubsub_v1

from .config import config

logger = logging.getLogger(__name__)

_clients_lock = threading.Lock()
_bigquery_client: bigquery.Client | None = None
_pubsub_publisher: pubsub_v1.PublisherClient | None = None


def get_bigquery_client() -> bigquery.Client:
    """The instance's shared BigQuery client (lazy, thread-safe)."""
    global _bigquery_client
    if _bigquery_client is None:
        with _clients_lock:
            if _bigquery_client is None:
                _bigquery_client = bigquery.Client(project=config.GOOGLE_CLOUD_PROJECT)
    return _bigquery_client


def get_pubsub_client() -> pubsub_v1.PublisherClient:
    """The instance's shared Pub/Sub publisher (lazy, thread-safe)."""
    global _pubsub_publisher
    if _pubsub_publisher is None:
        with _clients_lock:
            if _pubsub_publisher is None:
                _pubsub_publisher = pubsub_v1.PublisherClient()
    return _pubsub_publisher


def new_vertex_client():
    """A new Agent Runtime client (``agentplatform.Client``) for ONE invocation.

    Deliberately not cached: the worker drives each invocation with its own
    ``asyncio.run`` (a new event loop), and without aiohttp (not in this image)
    google-genai's async path reuses a single ``httpx.AsyncClient`` bound to
    the loop of its first request, so a cached client made the 2nd invocation
    on a warm instance fail with ``RuntimeError: Event loop is closed``.
    Creating one is cheap (no network call); ``create_agent_run`` closes it
    (``aio.aclose()``) before its loop ends. Lazy, so importing this module
    needs no credentials.
    """
    return agentplatform.Client(
        project=config.GOOGLE_CLOUD_PROJECT,
        location=config.GCP_REGION,
    )  # pyright: ignore[reportCallIssue]


async def aclose_vertex_client(client) -> None:
    """Release the client's async HTTP transport on the current loop (best
    effort: a close failure is logged, never fails the run)."""
    try:
        # The SDK's AsyncClient.aclose is a coroutine function (pinned by
        # tests/test_crf_time_budget.py), so it is awaited directly.
        await client.aio.aclose()
    except Exception as e:  # noqa: BLE001 — cleanup only
        logger.warning(f"Closing the Agent Runtime client failed: {e!r}")
