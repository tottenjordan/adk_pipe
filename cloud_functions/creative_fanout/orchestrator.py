"""The orchestrator's fan-out: one worker message per unprocessed trend row,
sent over ``CRF_WORKER_TRANSPORT``: a Pub/Sub message on the worker topic
(``pubsub``, the default) or a Cloud Tasks HTTP task (``tasks``)."""

import hashlib
import json
import logging
from datetime import datetime, timedelta

from google.api_core import exceptions as gexc
from google.cloud import tasks_v2

from . import clients, status_store
from .config import config
from .messages import build_row_dict

logger = logging.getLogger(__name__)

# How long to wait for each worker publish to resolve.
PUBLISH_TIMEOUT_SECS = 30.0
# How long each Cloud Tasks create_task call may take.
CREATE_TASK_TIMEOUT_SECS = 30.0


def _worker_topic_path() -> str:
    """Fully-qualified worker topic, resolved at use time (the project is a
    required env var, so resolving it at import would break credential-free
    imports)."""
    return (
        f"projects/{config.GOOGLE_CLOUD_PROJECT_NUMBER}"
        f"/topics/{config.CREATIVE_WORKER_TOPIC_NAME}"
    )


def _resolve_max_rows(requested, cap):
    """The trigger message's optional ``max_rows``, clamped to ``[1, cap]``.

    Unset or invalid (non-integer, non-positive, bool) falls back to ``cap``.
    """
    if requested is None or isinstance(requested, bool):
        return cap
    try:
        value = int(requested)
    except (TypeError, ValueError):
        logger.warning(f"Ignoring invalid max_rows={requested!r}; using {cap}.")
        return cap
    if value < 1:
        logger.warning(f"Ignoring non-positive max_rows={value}; using {cap}.")
        return cap
    return min(value, cap)


def dispatch(payload: dict) -> int:
    """Reap → select → mark QUEUED → publish → count, for one validated
    trigger payload (``messages.parse_trigger_message``).

    Returns the number of worker messages dispatched (published, or tasks
    created/already existing). A BigQuery error raises (the trigger NACKs and
    retries); a failed publish or task is only logged.
    """
    dataset = payload["bq_dataset"]
    table = payload["bq_table"]

    bq_client = clients.get_bigquery_client()

    # 0. Reap stale PROCESSING rows: a worker that hard-crashes AFTER acquiring
    # the lock strands its row in PROCESSING forever (a clean failure self-reports
    # FAILED). Re-queue those older than the staleness bound under the attempt cap
    # so the re-query below re-dispatches them (or fail them over the cap).
    status_store.reap_stale_processing_rows(bq_client, dataset, table)

    # 1. Fetch unprocessed rows: brand-new (processed_status IS NULL) AND rows
    # orphaned in QUEUED by a prior run that set them QUEUED then crashed or only
    # partially published (a failed publish leaves the row QUEUED), plus rows the
    # reap step above just re-queued. Re-dispatch is safe: a row a worker already
    # grabbed is PROCESSING/PROCESSED (not selected here), and a genuinely-stuck
    # QUEUED row is re-sent — the worker's atomic QUEUED->PROCESSING lock
    # (acquire_processing_lock) dedups any double delivery.
    # Capped at CRF_MAX_ROWS_PER_RUN (oldest first) so one trigger can't fan
    # out the whole backlog; rows past the limit stay unclaimed for next time.
    max_rows = _resolve_max_rows(payload.get("max_rows"), config.CRF_MAX_ROWS_PER_RUN)
    rows = status_store.select_unprocessed(bq_client, dataset, table, max_rows)

    if not rows:
        logging.info(
            "No new rows found to process (all are marked PROCESSED or table is empty)."
        )
        return 0

    # 2. Extract data into a list of dictionaries
    row_list = [build_row_dict(index, row) for index, row in enumerate(rows)]
    logging.info(f"Loaded {len(row_list)} UNPROCESSED records for agent execution.")

    # 3. The rows' keys (uuid, target_trend).
    row_keys = [status_store.RowKey.from_mapping(d) for d in row_list]

    # --- LOCK STEP ---
    # 4. Mark the batch 'QUEUED' BEFORE the first publish/task: the worker's lock
    # only takes a QUEUED row, so a brand-new (NULL) row published unmarked
    # would never run. Only NULL/QUEUED rows change, so a row a worker locked
    # or finished since the SELECT is never set back to QUEUED (its message is
    # a no-op). If this raises, the trigger NACKs and retries.
    status_store.mark_queued(bq_client, dataset, table, row_keys)

    # 5. Dispatch: one worker message per row, over the configured transport
    # (Pub/Sub topic or Cloud Tasks queue). A row whose send fails stays
    # QUEUED and is re-selected by the next orchestrator run, so a failure is
    # only surfaced, never fatal for the batch.
    if config.WORKER_TRANSPORT == "tasks":
        dispatched_count, failed_count = _send_tasks(dataset, table, rows, row_list)
        unit = "tasks"
    else:
        dispatched_count, failed_count = _send_pubsub(dataset, table, row_list)
        unit = "publishes"

    if failed_count:
        logging.warning(
            f"{failed_count}/{len(row_list)} worker {unit} failed; those rows "
            "remain QUEUED and will be recovered on the next orchestrator run."
        )
    logging.info(
        f"Successfully dispatched {dispatched_count} tasks to the worker "
        f"({config.WORKER_TRANSPORT})."
    )
    return dispatched_count


def _worker_payload(dataset: str, table: str, row_dict: dict) -> dict:
    """The worker message (Pub/Sub data or Cloud Tasks body; identical on both
    transports). No engine id: the worker runs its CREATIVE_AGENT_ENGINE_ID."""
    return {"bq_dataset": dataset, "bq_table": table, "row_data": row_dict}


def _send_pubsub(dataset: str, table: str, row_list: list[dict]) -> tuple[int, int]:
    """Publish one message per row to the worker topic, then confirm each
    publish resolved. Returns ``(dispatched, failed)``."""
    pubsub_publisher = clients.get_pubsub_client()
    worker_topic = _worker_topic_path()
    publish_futures = [
        pubsub_publisher.publish(
            worker_topic,
            json.dumps(_worker_payload(dataset, table, row_dict)).encode("utf-8"),
        )
        for row_dict in row_list
    ]

    dispatched_count = 0
    failed_count = 0
    for future in publish_futures:
        try:
            # Bounded: an unbounded wait can block for the publisher's whole
            # retry deadline (about the orchestrator's timeout). A publish that
            # times out leaves its row QUEUED, recovered by the next re-query.
            future.result(timeout=PUBLISH_TIMEOUT_SECS)
            dispatched_count += 1
        except Exception as e:  # noqa: BLE001 — one bad publish shouldn't sink the batch
            failed_count += 1
            logging.error(f"Failed to publish a worker message: {e}")
    return dispatched_count, failed_count


def _queue_path() -> str:
    return tasks_v2.CloudTasksClient.queue_path(
        config.GOOGLE_CLOUD_PROJECT, config.TASKS_LOCATION, config.TASKS_QUEUE
    )


def task_name(parent: str, row) -> str:
    """The deterministic Cloud Tasks name of one row *attempt*.

    ``crf-<sha256(uuid, target_trend, processing_attempts or 0)[:32]>`` (a
    legacy row without a key: its ``entry_timestamp`` instead of the key).
    Cloud Tasks refuses a second task with the same name (``AlreadyExists``,
    also for about an hour after the first one ran), so an orphaned QUEUED row
    re-selected while its task still waits in the queue is not enqueued twice.
    The attempt count changes on every lock, so a reaped or re-queued row gets
    a fresh task.
    """
    key = status_store.RowKey.from_mapping(row)
    attempts = row.get("processing_attempts") or 0
    if key.legacy:
        ts = key.entry_timestamp
        parts = [ts.isoformat() if isinstance(ts, datetime) else str(ts), attempts]
    else:
        parts = [key.uuid, key.target_trend, attempts]
    digest = hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()
    return f"{parent}/tasks/crf-{digest[:32]}"


def _send_tasks(
    dataset: str, table: str, rows: list, row_list: list[dict]
) -> tuple[int, int]:
    """Create one Cloud Tasks HTTP task per row (OIDC-authenticated POST of the
    worker message to ``CRF_WORKER_URL``). Returns ``(dispatched, failed)``.

    ``AlreadyExists`` = this row attempt already has a task: dispatched. Any
    other error is logged and counted; the row stays QUEUED.
    """
    tasks_client = clients.get_tasks_client()
    parent = _queue_path()
    deadline = timedelta(seconds=int(config.TASK_DISPATCH_DEADLINE_SECS))
    dispatched_count = 0
    failed_count = 0
    for row, row_dict in zip(rows, row_list, strict=True):
        name = task_name(parent, row)
        body = json.dumps(_worker_payload(dataset, table, row_dict)).encode("utf-8")
        task = {
            "name": name,
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": config.WORKER_URL,
                "headers": {"Content-Type": "application/json"},
                "body": body,
                "oidc_token": {
                    "service_account_email": config.TASKS_INVOKER_SA,
                    "audience": config.WORKER_URL,
                },
            },
            "dispatch_deadline": deadline,
        }
        try:
            tasks_client.create_task(
                request={"parent": parent, "task": task},
                timeout=CREATE_TASK_TIMEOUT_SECS,
            )
            dispatched_count += 1
        except gexc.AlreadyExists:
            logging.info(f"Worker task {name} already exists; not enqueued again.")
            dispatched_count += 1
        except Exception as e:  # noqa: BLE001 — one bad task shouldn't sink the batch
            failed_count += 1
            logging.error(f"Failed to create worker task {name}: {e}")
    return dispatched_count, failed_count
