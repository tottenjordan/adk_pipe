"""The orchestrator's fan-out: one worker message per unprocessed trend row."""

import json
import logging

from . import clients, status_store
from .config import config
from .messages import build_row_dict

logger = logging.getLogger(__name__)

# How long to wait for each worker publish to resolve.
PUBLISH_TIMEOUT_SECS = 30.0


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

    Returns the number of worker messages published. A BigQuery error raises
    (the trigger NACKs and retries); a failed publish is only logged.
    """
    dataset = payload["bq_dataset"]
    table = payload["bq_table"]

    bq_client = clients.get_bigquery_client()
    pubsub_publisher = clients.get_pubsub_client()

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
    # 4. Mark the batch 'QUEUED' BEFORE the first publish: the worker's lock
    # only takes a QUEUED row, so a brand-new (NULL) row published unmarked
    # would never run. Only NULL/QUEUED rows change, so a row a worker locked
    # or finished since the SELECT is never set back to QUEUED (its message is
    # a no-op). If this raises, the trigger NACKs and retries.
    status_store.mark_queued(bq_client, dataset, table, row_keys)

    # 5. Dispatch Step: Publish one message per row to the worker topic, then
    # confirm each publish resolved. A publish that fails silently would leave
    # its row QUEUED forever under the old IS-NULL-only re-query; now such a row
    # is recovered by the broadened re-query on the next orchestrator run, so we
    # only need to surface the failure (don't crash the whole batch over one).
    worker_topic = _worker_topic_path()
    publish_futures = []
    for row_dict in row_list:
        # Create a dedicated payload for the worker. No engine id: the
        # worker runs the engine in its CREATIVE_AGENT_ENGINE_ID env var.
        worker_payload = {
            "bq_dataset": dataset,
            "bq_table": table,
            "row_data": row_dict,  # Pass the necessary row info
        }

        data_str = json.dumps(worker_payload)
        data_bytes = data_str.encode("utf-8")
        publish_futures.append(pubsub_publisher.publish(worker_topic, data_bytes))

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

    if failed_count:
        logging.warning(
            f"{failed_count}/{len(row_list)} worker publishes failed; those rows "
            "remain QUEUED and will be recovered on the next orchestrator run."
        )
    logging.info(
        f"Successfully dispatched {dispatched_count} tasks to the worker queue."
    )
    return dispatched_count
