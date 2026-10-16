"""The orchestrator's fan-out: one Cloud Tasks HTTP task (the worker message)
per unprocessed trend row. Cloud Tasks is the only transport: without a usable
worker URL and invoker service account nothing is sent (never a fallback)."""

import hashlib
import json
import logging
from datetime import datetime, timedelta

from google.api_core import exceptions as gexc
from google.api_core.retry import Retry, if_exception_type
from google.cloud import tasks_v2

from . import clients, status_store
from .config import config
from .messages import build_row_dict

logger = logging.getLogger(__name__)

# How long each Cloud Tasks create_task call may take.
CREATE_TASK_TIMEOUT_SECS = 30.0
# create_task has no default retry (unlike get_task); retry the transient
# errors for up to 30 s. Safe: a named task is never created twice.
CREATE_TASK_RETRY = Retry(
    predicate=if_exception_type(gexc.ServiceUnavailable, gexc.DeadlineExceeded),
    initial=1,
    maximum=10,
    timeout=30,
)


def _tasks_config_error() -> str | None:
    """Why the Cloud Tasks settings can't send a task, or None when they can.

    A task needs an ``https://`` ``CRF_WORKER_URL`` (its target and OIDC
    audience) and an invoker service account (the OIDC token's identity:
    ``CRF_TASKS_INVOKER_SA``, or ``GOOGLE_CLOUD_PROJECT_NUMBER`` for the
    default compute SA).
    """
    if not config.WORKER_URL.startswith("https://"):
        return f"CRF_WORKER_URL must be an https:// URL (got {config.WORKER_URL!r})"
    if not config.TASKS_INVOKER_SA:
        return (
            "no invoker service account (set CRF_TASKS_INVOKER_SA, or "
            "GOOGLE_CLOUD_PROJECT_NUMBER for the default compute SA)"
        )
    return None


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
    """Reap → select → mark QUEUED → create tasks → count, for one validated
    trigger payload (``messages.parse_trigger_message``).

    Returns the number of worker tasks dispatched (created or already
    existing). Unusable Cloud Tasks settings log an ERROR and return 0 before
    any BigQuery work, so every row keeps its status (a QUEUED row stays
    QUEUED) for the trigger after the fix. A BigQuery error raises (the
    trigger NACKs and retries); a failed task is only logged.
    """
    problem = _tasks_config_error()
    if problem:
        logger.error(f"Not dispatching any worker task: {problem}.")
        return 0

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
    # partially dispatched (a failed task leaves the row QUEUED), plus rows the
    # reap step above just re-queued. Re-dispatch is safe: a row a worker already
    # grabbed is PROCESSING/PROCESSED (not selected here), and a genuinely-stuck
    # QUEUED row is re-sent — the worker's atomic QUEUED->PROCESSING lock
    # (acquire_processing_lock) dedups any double delivery.
    # Capped at CRF_MAX_ROWS_PER_RUN (oldest first) so one trigger can't fan
    # out the whole backlog; rows past the limit stay unclaimed for next time.
    max_rows = _resolve_max_rows(payload.get("max_rows"), config.CRF_MAX_ROWS_PER_RUN)
    rows = status_store.select_unprocessed(bq_client, dataset, table, max_rows)

    if not rows:
        logger.info(
            "No new rows found to process (all are marked PROCESSED or table is empty)."
        )
        return 0

    # 2. Extract data into a list of dictionaries
    row_list = [build_row_dict(index, row) for index, row in enumerate(rows)]
    logger.info(f"Loaded {len(row_list)} UNPROCESSED records for agent execution.")

    # 3. The rows' keys (uuid, target_trend).
    row_keys = [status_store.RowKey.from_mapping(d) for d in row_list]

    # --- LOCK STEP ---
    # 4. Mark the batch 'QUEUED' BEFORE the first task: the worker's lock
    # only takes a QUEUED row, so a brand-new (NULL) row dispatched unmarked
    # would never run. Only NULL/QUEUED rows change, so a row a worker locked
    # or finished since the SELECT is never set back to QUEUED (its message is
    # a no-op). If this raises, the trigger NACKs and retries.
    status_store.mark_queued(bq_client, dataset, table, row_keys)

    # 5. Dispatch: one Cloud Tasks task per row. A row whose task fails stays
    # QUEUED and is re-selected by the next orchestrator run, so a failure is
    # only surfaced, never fatal for the batch.
    dispatched_count, failed_count = _send_tasks(dataset, table, rows, row_list)

    if failed_count:
        logger.warning(
            f"{failed_count}/{len(row_list)} worker tasks failed; those rows "
            "remain QUEUED and will be recovered on the next orchestrator run."
        )
    logger.info(f"Successfully dispatched {dispatched_count} tasks to the worker.")
    return dispatched_count


def _worker_payload(dataset: str, table: str, row_dict: dict) -> dict:
    """The worker message (the Cloud Tasks body). No engine id: the worker
    runs its CREATIVE_AGENT_ENGINE_ID."""
    return {"bq_dataset": dataset, "bq_table": table, "row_data": row_dict}


def _queue_path() -> str:
    return tasks_v2.CloudTasksClient.queue_path(
        config.GOOGLE_CLOUD_PROJECT, config.TASKS_LOCATION, config.TASKS_QUEUE
    )


def task_name(parent: str, row) -> str:
    """The deterministic Cloud Tasks name of one row *attempt*.

    ``crf-<sha256(uuid, target_trend, processing_attempts or 0)[:32]>`` (a
    legacy row without a key: its ``entry_timestamp`` instead of the key).
    Cloud Tasks refuses a second task with the same name (``AlreadyExists``),
    also for up to 24 h after the first one ran or was deleted (9 days for a
    queue created from queue.yaml; ``tasks_v2`` ``Task.name``), so an orphaned
    QUEUED row re-selected while its task still waits in the queue is not
    enqueued twice (``_send_tasks`` re-creates a gone one unnamed). The
    attempt count changes on every lock, so a reaped or re-queued row gets a
    fresh task.
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

    ``create_task`` retries ``ServiceUnavailable`` / ``DeadlineExceeded``
    (``CREATE_TASK_RETRY``; safe, the task is named). ``AlreadyExists`` is
    confirmed with ``get_task``: a task still there means this row attempt is
    already queued (dispatched); ``NotFound`` means the name is only a
    tombstone (the task ran or was deleted without moving the row on), so the
    task is created again without a name (WARNING, ``task_name_tombstoned``);
    the worker's QUEUED->PROCESSING lock makes any duplicate a no-op. Any
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
            _create_task(tasks_client, parent, task)
            dispatched_count += 1
        except gexc.AlreadyExists:
            if _recreate_if_tombstoned(tasks_client, parent, task):
                dispatched_count += 1
            else:
                failed_count += 1
        except Exception as e:  # noqa: BLE001 — one bad task shouldn't sink the batch
            failed_count += 1
            logger.error(f"Failed to create worker task {name}: {e}")
    return dispatched_count, failed_count


def _create_task(tasks_client, parent: str, task: dict) -> None:
    tasks_client.create_task(
        request={"parent": parent, "task": task},
        retry=CREATE_TASK_RETRY,
        timeout=CREATE_TASK_TIMEOUT_SECS,
    )


def _recreate_if_tombstoned(tasks_client, parent: str, task: dict) -> bool:
    """After ``AlreadyExists`` for ``task``: True when the row attempt is
    dispatched (the named task still exists, or it was gone and an unnamed
    copy was created); False (logged) when that can't be confirmed or the
    unnamed create failed."""
    name = task["name"]
    try:
        tasks_client.get_task(name=name)
        logger.info(f"Worker task {name} already exists; not enqueued again.")
        return True
    except gexc.NotFound:
        pass
    except Exception as e:  # noqa: BLE001 — leave the row QUEUED for next time
        logger.error(f"Worker task {name} exists, but get_task failed: {e}")
        return False
    logger.warning(
        f"Worker task name {name} is taken by a task that is gone; creating "
        "the task again without a name.",
        extra={"json_fields": {"reason": "task_name_tombstoned", "task_name": name}},
    )
    unnamed = {k: v for k, v in task.items() if k != "name"}
    try:
        _create_task(tasks_client, parent, unnamed)
        return True
    except Exception as e:  # noqa: BLE001 — leave the row QUEUED for next time
        logger.error(f"Failed to create worker task {name} (unnamed): {e}")
        return False
