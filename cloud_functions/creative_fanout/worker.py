"""The worker's single-row run: lock the row, run the agent, record the status."""

import asyncio
import hashlib
import json
import logging
import time

from . import clients, status_store
from .completion import RunMonitor, RunOutcome
from .config import config
from .logs import pretty_print_event
from .session import agent_session

logger = logging.getLogger(__name__)

# Indirection so tests drive the completion check's polling without waiting.
# `main.agent_worker_entrypoint` takes the run deadline from `_clock` too.
_clock = time.monotonic
_sleep = asyncio.sleep


def run_deadline(request_start: float) -> float:
    """The ``_clock()`` time by which the run (stream + completion check + one
    retry) must be over: request start + the Cloud Run timeout - the safety
    margin kept for the session delete and the status write. Taken at request
    start (Cloud Run's timeout counts from there), never after the lock."""
    return request_start + config.WORKER_TIMEOUT_SECS - config.BUDGET_SAFETY_SECS


# Session-state key ← message key. The campaign fields plus trend_scout's
# detection date (`refresh_date` → `trend_detected_on`) and research context
# (`trend_context` → `trend_scout_context`), which the creative agents read only
# as hints to re-verify.
CAMPAIGN_STATE_KEYS = (
    ("brand", "brand"),
    ("target_product", "target_product"),
    ("key_selling_points", "key_selling_point"),
    ("target_audience", "target_audience"),
    ("target_search_trends", "target_search_trend"),
    ("trend_detected_on", "refresh_date"),
    ("trend_scout_context", "trend_context"),
)

# Kick-off message line label ← message key (a readable echo of the state).
USER_QUERY_LINES = (
    ("Brand", "brand"),
    ("Target Product", "target_product"),
    ("Key Selling Point(s)", "key_selling_point"),
    ("Target Audience", "target_audience"),
    ("Target Search Trend", "target_search_trend"),
)


def build_campaign_state(msg_dict: dict) -> dict[str, str]:
    """The createSession initialState for one row (blank/NULL values left out).

    Blank/NULL columns are skipped so the root memorizes the campaign fields
    from the message instead of treating a seeded "None" as present (and so an
    older row without trend_context/refresh_date seeds nothing for them).
    """
    return {
        state_key: str(msg_dict[msg_key]).strip()
        for state_key, msg_key in CAMPAIGN_STATE_KEYS
        if msg_dict.get(msg_key) is not None and str(msg_dict[msg_key]).strip()
    }


def build_user_query(msg_dict: dict) -> str:
    """The kick-off message: one ``Label: value`` line per campaign field, with
    no leading indentation."""
    return "\n".join(f"{label}: {msg_dict[key]}" for label, key in USER_QUERY_LINES)


def _row_hash(*parts: str) -> str:
    """Copy of ``agent_common.idempotency.stable_row_id(*parts, length=12)``
    (the CRF image doesn't ship agent_common)."""
    return hashlib.sha256(json.dumps(list(parts)).encode()).hexdigest()[:12]


def session_user_id(row: dict) -> str:
    """The Agent Engine session user id for one row, from the row's identity.

    ``<AGENT_WORKER_USER_ID>_<hash(uuid, target_trend)>`` (trend_scout's row
    key) when the message carries both, so the id names the row, not its
    position in the batch; older messages without them fall back to
    ``<AGENT_WORKER_USER_ID>_<index>``.
    """
    base = config.AGENT_WORKER_USER_ID
    uuid, trend = row.get("uuid"), row.get("target_trend")
    if isinstance(uuid, str) and uuid.strip() and isinstance(trend, str) and trend:
        return f"{base}_{_row_hash(uuid.strip(), trend)}"
    return f"{base}_{row['index']}"


async def create_agent_run(
    agent_id: str,
    msg_dict: dict,
    user_id: str,
    *,
    deadline: float,
) -> RunOutcome:
    """Invoke agent workflow and report whether the run finished.

    Args:
        agent_id (str): resource ID for the deployed agent
        msg_dict (dict): campaign metadata for the creative agent. Has following keys:
            brand: brand to generate creatives for
            target_audience: description of target audience
            target_product: product to promote in creatives
            key_selling_point: compelling features etc. to reference
            target_search_trend: trending search topic to overlap with creative themes
            refresh_date (optional): ISO date trend_scout detected the trend
            trend_context (optional): trend_scout's compact research JSON
        user_id (str): the user ID to use for the Agent Engine session
        deadline (float): ``_clock()`` time by which the run must be over
            (``run_deadline`` of the request start; the session delete and the
            status write use the safety margin after it)

    Returns:
        RunOutcome: ``finalized`` only when the session reached
        ``finalize_done`` (see completion.py: a clean stream end is not
        enough; a timeout/cut is polled; ONE retry message only once the
        session shows the invocation ended unfinished).
    """
    logger.info(
        f"Invoking agent run for row {msg_dict.get('entry_timestamp')} "
        f"(user ID {user_id}).",
        extra={
            "json_fields": {
                "row": msg_dict.get("entry_timestamp"),
                "uuid": msg_dict.get("uuid"),
                "target_trend": msg_dict.get("target_trend"),
                "index": msg_dict.get("index"),
                "user_id": user_id,
                "budget_secs": round(deadline - _clock(), 1),
            }
        },
    )

    # Seed the campaign fields as session state so the inputs are deterministic
    # (creative_agent's state init setdefaults them; the root only memorizes
    # fields missing from state). The message stays as a readable echo.
    campaign_state = build_campaign_state(msg_dict)

    # create → stream + completion check → delete, all under one user_id. The
    # delete runs only after the check (it reads the session) and on every path
    # (agent_session's finally, best effort: bounded, never raises), so a
    # failed run never leaks a session and a failed delete never changes the
    # outcome. A fresh client (and engine handle) per invocation, closed on
    # this loop: see clients.new_vertex_client for why it can't be cached.
    client = clients.new_vertex_client()
    try:
        remote_agent = client.runtimes.get(
            name=(
                f"projects/{config.GOOGLE_CLOUD_PROJECT_NUMBER}/locations/"
                f"{config.GCP_REGION}/reasoningEngines/{agent_id}"
            )
        )
        async with agent_session(
            remote_agent,
            user_id,
            state=campaign_state,
            delete_timeout_secs=config.SESSION_DELETE_TIMEOUT_SECS,
        ) as session:
            # long running op
            monitor = RunMonitor(
                remote_agent,
                user_id,
                session["id"],
                deadline=deadline,
                stream_timeout_secs=config.STREAM_TIMEOUT_SECS,
                poll_interval_secs=config.POLL_INTERVAL_SECS,
                idle_secs=config.IDLE_SECS,
                read_timeout_secs=config.SESSION_READ_TIMEOUT_SECS,
                root_author=config.ROOT_AGENT_NAME,
                clock=_clock,
                sleep=_sleep,
                on_event=pretty_print_event,
            )
            return await monitor.run(build_user_query(msg_dict))
    finally:
        await clients.aclose_vertex_client(client)


class RunNotFinalizedError(RuntimeError):
    """The run ended (after at most one retry) without ``finalize_done``."""

    def __init__(self, outcome: RunOutcome):
        super().__init__(
            f"run not finalized (reason={outcome.reason}, retried={outcome.retried})"
        )
        self.outcome = outcome


def _log_run_duration(
    timestamp, index, status, run_start, outcome: RunOutcome | None = None
) -> None:
    """The AGENT_RUN_DURATION_SECS marker: the same text as before (existing
    log queries match ``message``) plus row/index/status/secs and the
    completion check's finalized/retried/reason as JSON keys."""
    secs = round(_clock() - run_start, 1)
    fields = {"row": timestamp, "index": index, "status": status, "secs": secs}
    fields["finalized"] = outcome.finalized if outcome else False
    fields["retried"] = outcome.retried if outcome else False
    fields["reason"] = outcome.reason if outcome else "error"
    logger.info(
        f"AGENT_RUN_DURATION_SECS row={timestamp} index={index} "
        f"status={status} secs={secs:.1f}",
        extra={"json_fields": fields},
    )


async def process_row(
    trend_dict, agent_id, bq_client, dataset, table, *, deadline: float
):
    """Handles the async agent run for a single row and updates its status."""

    # Extract data
    timestamp = trend_dict["entry_timestamp"]

    # 1. ATTEMPT TO ACQUIRE ATOMIC LOCK
    if not status_store.acquire_processing_lock(bq_client, dataset, table, timestamp):
        # Lock failed. Another worker got it, or the row is already PROCESSED/FAILED.
        # RETURN here so the Pub/Sub message is ACKed and no more processing attempts are made.
        return

    # --- ONLY ONE WORKER PROCEEDS PAST THIS POINT ---

    # Wall-clock timer for the end-to-end agent run. Emitted as a structured log
    # marker (AGENT_RUN_DURATION_SECS) on both the success and failure paths so we
    # can compute p50/p95 pipeline duration from Cloud Logging — the key input to
    # the Ambient-Agent trigger-endpoint decision (10-min ceiling). See
    # docs/notes/ambient-agents-vs-cloud-functions.md.
    run_start = _clock()
    outcome: RunOutcome | None = None
    try:
        # 2. Run the Agent (The heavy async part) and check it finished
        # (finalize_done; at most one retry). The row status is now 'PROCESSING'.
        user_id = session_user_id(trend_dict)
        outcome = await create_agent_run(
            agent_id=agent_id,
            msg_dict=trend_dict,
            user_id=user_id,
            deadline=deadline,
        )
        if not outcome.finalized:
            raise RunNotFinalizedError(outcome)

        # 3. Update status to PROCESSED
        # We MUST change the status away from PROCESSING now
        status_store.update_rows_status(
            bq_client=bq_client,
            dataset=dataset,
            table=table,
            timestamps=[timestamp],
            status="PROCESSED",
        )
        _log_run_duration(
            timestamp, trend_dict["index"], "PROCESSED", run_start, outcome
        )
        logging.info(f"Successfully processed and marked row {timestamp} as PROCESSED.")

    except Exception as e:  # noqa: BLE001 — recorded as FAILED below, then ACKed
        # The agent run (or the PROCESSED write) failed: record FAILED, then
        # RETURN so the worker message is ACKed. Re-raising would only buy a
        # no-op retry: the redelivery's QUEUED->PROCESSING lock can't match a
        # FAILED row, so it would just log "Lock failed" and ACK anyway.
        _log_run_duration(timestamp, trend_dict["index"], "FAILED", run_start, outcome)
        logger.error(
            f"Failed processing row {timestamp} (index={trend_dict['index']}): "
            f"{type(e).__name__}: {e}",
            extra={
                "json_fields": {
                    "row": timestamp,
                    "status": "FAILED",
                    "reason": (
                        e.outcome.reason
                        if isinstance(e, RunNotFinalizedError)
                        else type(e).__name__
                    ),
                    "retried": outcome.retried if outcome else False,
                    "secs": round(_clock() - run_start, 1),
                }
            },
        )

        # 4. Update status PROCESSING -> FAILED. If THIS write raises, it
        # propagates (failed invocation / NACK) so the error is loud. The row is
        # left in PROCESSING, which the orchestrator's stale-PROCESSING reaper
        # re-queues (or fails, over the attempt cap) after
        # REAP_STALE_PROCESSING_MINUTES.
        status_store.update_rows_status(
            bq_client=bq_client,
            dataset=dataset,
            table=table,
            timestamps=[timestamp],
            status="FAILED",
        )
        logger.error(
            f"Marked row {timestamp} as FAILED; ACKing the worker message "
            "(no retry: a FAILED row can't be re-locked)."
        )
        return
