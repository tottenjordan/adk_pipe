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
from .messages import build_row_dict
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
    # Row state (C1): an error before the session exists (runtimes.get,
    # async_create_session) is re-raised as SessionNotStarted, so process_row
    # can re-queue the row on a transient one instead of failing it.
    session_started = False
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
            session_started = True
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
    except Exception as e:
        if not session_started:
            raise SessionNotStarted(f"no session created: {e!r}") from e
        raise
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


class SessionNotStarted(RuntimeError):
    """``create_agent_run`` failed before an Agent Engine session existed
    (``runtimes.get`` or ``async_create_session``): nothing ran and nothing was
    spent. The original error is ``__cause__``; ``process_row`` re-queues the
    row when it is transient (``_is_transient``)."""


def _transient_types() -> tuple[type[BaseException], ...]:
    types: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)
    try:  # the genai/agentplatform transport; optional so tests import without it
        import httpx

        types += (httpx.TimeoutException, httpx.NetworkError)
    except ImportError:  # pragma: no cover
        pass
    return types


_TRANSIENT_TYPES = _transient_types()


def _is_transient(exc: BaseException | None) -> bool:
    """A timeout, a connection error or an HTTP 429 / 5xx anywhere in the
    cause chain (``code`` is the HTTP status on google-api-core and genai /
    agentplatform errors)."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, _TRANSIENT_TYPES):
            return True
        code = getattr(exc, "code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            if code == 429 or 500 <= code <= 599:
                return True
        exc = exc.__cause__ or exc.__context__
    return False


# The PROCESSED write (an idempotent, fenced UPDATE) is retried this many times
# in all, with a linear backoff, while a bounded BigQuery wait still fits.
PROCESSED_WRITE_ATTEMPTS = 3
PROCESSED_WRITE_BACKOFF_SECS = 2.0


async def _write_processed(bq_client, dataset, table, key, attempt, deadline):
    """Record PROCESSED for a run that reached ``finalize_done``.

    Never FAILED: a finished run's outputs exist, so a write that keeps failing
    is logged (``reason=status_write_failed``) and the row left in PROCESSING
    for the reaper (at-least-once). The first try always runs (the safety
    margin after ``deadline`` covers one); a retry only while one more bounded
    BigQuery wait still fits before the Cloud Run timeout
    (``deadline + BUDGET_SAFETY_SECS``).
    """
    hard_end = deadline + config.BUDGET_SAFETY_SECS
    for n in range(1, PROCESSED_WRITE_ATTEMPTS + 1):
        try:
            status_store.mark_final(
                bq_client, dataset, table, key, "PROCESSED", attempt
            )
            return
        except Exception as e:  # noqa: BLE001 — retried, then logged, never FAILED
            backoff = PROCESSED_WRITE_BACKOFF_SECS * n
            fits = hard_end - _clock() - backoff >= config.BQ_QUERY_TIMEOUT_SECS
            if n == PROCESSED_WRITE_ATTEMPTS or not fits:
                logger.error(
                    f"PROCESSED write failed for finished row {key} "
                    f"(attempt {attempt}, try {n}): {type(e).__name__}: {e}; "
                    "leaving it PROCESSING for the reaper.",
                    extra={
                        "json_fields": {
                            **key.log_fields(),
                            "reason": "status_write_failed",
                            "status": "PROCESSING",
                            "attempt": attempt,
                        }
                    },
                )
                return
            logger.warning(
                f"PROCESSED write failed for row {key} (try {n}), retrying: {e}"
            )
            await _sleep(backoff)


async def process_row(
    trend_dict, agent_id, bq_client, dataset, table, *, deadline: float
):
    """Lock one row, re-read it, run the agent on it and record the outcome.

    The worker message only names the row: its key (``uuid``, ``target_trend``;
    a legacy message without them is matched by ``entry_timestamp`` once) and
    ``index``. The campaign fields come from the row re-read after the lock.

    Returns (ACK) on every outcome: lost lock, missing/sensitive row (FAILED),
    finished run (PROCESSED, or left PROCESSING when that write keeps failing),
    unfinished run (FAILED), transient error before a session existed (back to
    QUEUED). A BigQuery error on the lock, the re-read or the FAILED write
    raises (NACK; the redelivery's lock then finds the row not QUEUED, and the
    reaper is the backstop for a row left PROCESSING).
    """
    key = status_store.RowKey.from_mapping(trend_dict)
    index = trend_dict["index"]
    if key.legacy:
        logger.warning(
            f"Worker message for row {key} has no row key (uuid, target_trend): "
            "matching it by entry_timestamp once (sent before the row-key deploy).",
            extra={"json_fields": {**key.log_fields(), "reason": "legacy_row_key"}},
        )

    # 1. Compare-and-set lock QUEUED -> PROCESSING. The attempt it returns
    # fences every later write: a write matches only while the row is still
    # PROCESSING at this attempt.
    attempt = status_store.acquire_processing_lock(bq_client, dataset, table, key)
    if attempt is None:
        # Another worker has it, or it is PROCESSED/FAILED: ACK, run nothing.
        return

    # --- ONLY ONE WORKER PROCEEDS PAST THIS POINT ---

    # 2. Re-read the locked row and build the run from BigQuery, not from the
    # message: the sensitive-trend filter is re-checked now (a row flagged
    # after dispatch, or a hand-made message, never runs).
    row = status_store.read_locked_row(bq_client, dataset, table, key, attempt)
    if row is None or row.get("crf_sensitive"):
        reason = "row_missing" if row is None else "row_sensitive"
        logger.error(
            f"Not running row {key} (attempt {attempt}): {reason}; marking FAILED.",
            extra={"json_fields": {**key.log_fields(), "reason": reason}},
        )
        status_store.mark_final(bq_client, dataset, table, key, "FAILED", attempt)
        return
    key = status_store.RowKey.from_mapping(row)  # a legacy key gets the real one
    msg_dict = build_row_dict(index, row)
    timestamp = msg_dict["entry_timestamp"]

    # Wall-clock timer for the end-to-end agent run. Emitted as a structured log
    # marker (AGENT_RUN_DURATION_SECS) on both the success and failure paths so we
    # can compute p50/p95 pipeline duration from Cloud Logging — the key input to
    # the Ambient-Agent trigger-endpoint decision (10-min ceiling). See
    # docs/notes/ambient-agents-vs-cloud-functions.md.
    run_start = _clock()
    outcome: RunOutcome | None = None
    error: Exception | None = None
    try:
        # 3. Run the agent and check it finished (finalize_done; at most one
        # retry). The row status is now 'PROCESSING'.
        outcome = await create_agent_run(
            agent_id=agent_id,
            msg_dict=msg_dict,
            user_id=session_user_id(msg_dict),
            deadline=deadline,
        )
    except SessionNotStarted as e:
        if _is_transient(e.__cause__):
            # Nothing ran: put the row back (the lock already counted the
            # attempt, so MAX_PROCESSING_ATTEMPTS still caps the retries).
            status = status_store.requeue(bq_client, dataset, table, key, attempt)
            logger.warning(
                f"Transient error before a session existed for row {key} "
                f"(attempt {attempt}): {e.__cause__!r}; row -> {status}.",
                extra={
                    "json_fields": {
                        **key.log_fields(),
                        "reason": "session_not_started",
                        "status": status,
                        "attempt": attempt,
                    }
                },
            )
            return
        error = e
    except Exception as e:  # noqa: BLE001 — recorded as FAILED below, then ACKed
        error = e
    else:
        if not outcome.finalized:
            error = RunNotFinalizedError(outcome)

    if error is None:
        # 4. The run finished: PROCESSED (retried; never turned into FAILED).
        await _write_processed(bq_client, dataset, table, key, attempt, deadline)
        _log_run_duration(timestamp, index, "PROCESSED", run_start, outcome)
        return

    # The agent run failed: record FAILED, then RETURN so the worker message is
    # ACKed. Re-raising would only buy a no-op retry: the redelivery's
    # QUEUED->PROCESSING lock can't match a FAILED row.
    _log_run_duration(timestamp, index, "FAILED", run_start, outcome)
    if isinstance(error, RunNotFinalizedError):
        reason = error.outcome.reason
    elif isinstance(error, SessionNotStarted):
        reason = f"session_not_started:{type(error.__cause__).__name__}"
    else:
        reason = type(error).__name__
    logger.error(
        f"Failed processing row {key} (index={index}): {type(error).__name__}: {error}",
        extra={
            "json_fields": {
                **key.log_fields(),
                "status": "FAILED",
                "reason": reason,
                "retried": outcome.retried if outcome else False,
                "secs": round(_clock() - run_start, 1),
            }
        },
    )

    # 5. PROCESSING@attempt -> FAILED (fenced). If THIS write raises, it
    # propagates (failed invocation / NACK) so the error is loud; the row is
    # left in PROCESSING for the stale-PROCESSING reaper.
    if status_store.mark_final(bq_client, dataset, table, key, "FAILED", attempt):
        logger.error(
            f"Marked row {key} as FAILED; ACKing the worker message "
            "(no retry: a FAILED row can't be re-locked)."
        )
