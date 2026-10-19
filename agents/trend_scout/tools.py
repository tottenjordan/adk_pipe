import asyncio
import datetime
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from google.adk.tools import ToolContext
from google.cloud import bigquery

from agent_common import collect_degradation_warnings
from agent_common.clients import get_bigquery_client, get_gcs_client
from agent_common.clock import now_in_tz
from agent_common.idempotency import stable_row_id
from agent_common.state import memorize  # noqa: F401  (ADK tool; re-exported)

from .config import config
from .staleness import gtrends_staleness, staleness_note
from .trend_context import norm_term, sensitive_flags, trend_context_for

# ==============================
# clients
# =============================
# Shared lazy getters (agent_common.clients), bound to the historical private
# names so call sites + test monkeypatch points are unchanged.
_get_gcs_client = get_gcs_client
_get_bigquery_client = get_bigquery_client


# =============================
# tools
# =============================


def record_research_gaps(tool_context: ToolContext) -> dict:
    """Turn any RetryUntilKeyNode exhaustion markers into a human-readable note.

    Reads the `*__retry_exhausted` markers left in state by the resilient research
    wrapper and stores a single `research_gaps` string (empty when research was
    clean). This is trend_scout's analog of the creative gallery banner — the same
    `collect_degradation_warnings` source of truth — so a degraded run surfaces WHY
    in the handoff and the persisted GCS session JSON instead of only in logs.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: status, the `research_gaps` string, and the note `count`.
    """
    notes = collect_degradation_warnings(tool_context.state)
    research_gaps = "; ".join(notes)
    tool_context.state["research_gaps"] = research_gaps
    return {"status": "success", "research_gaps": research_gaps, "count": len(notes)}


# ==============================
# Google Search Trends (context)
# =============================
# Bounds on the public-table reads (like trend_scout/digest.py's lookup): the
# server-side job timeout plus a client-side wait a little longer than it.
GTRENDS_JOB_TIMEOUT_MS = 60_000
GTRENDS_RESULT_TIMEOUT_SECONDS = 75.0


def _gtrends_job_config(
    params: list[bigquery.ScalarQueryParameter] | None = None,
) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(
        query_parameters=params or [], job_timeout_ms=GTRENDS_JOB_TIMEOUT_MS
    )


def _get_gtrends_max_date() -> str:
    query = """
        SELECT
         MAX(refresh_date) as max_date
        FROM `bigquery-public-data.google_trends.top_terms`
    """
    bq_client = _get_bigquery_client()
    job = bq_client.query(query, job_config=_gtrends_job_config())
    row = next(iter(job.result(timeout=GTRENDS_RESULT_TIMEOUT_SECONDS)))
    return row["max_date"].strftime("%m/%d/%Y")


def _gathered_refresh_date(value: object) -> str:
    """State ``gtrends_refresh_date`` (ISO) as MM/DD/YYYY, or "" when unusable."""
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value.strip()).strftime("%m/%d/%Y")
        except ValueError:
            pass
    return ""


def _gtrends_markdown(rows: list[tuple[str, int, str]]) -> str:
    """Render (term, rank, refresh_date) rows as a Markdown pipe table."""
    lines = [
        "|    | term | rank | refresh_date |",
        "|---:|:-----|-----:|:-------------|",
    ]
    lines += [
        f"| {rank} | {term} | {rank} | {refresh_date} |"
        for term, rank, refresh_date in rows
    ]
    return "\n".join(lines)


def _query_daily_gtrends() -> tuple[str, list[tuple[str, int, str]]]:
    """Blocking BigQuery reads behind get_daily_gtrends: (max_date, rows)."""
    # get latest refresh date
    max_date = _get_gtrends_max_date()
    logging.info(f"\n\nmax_date in trends_assistant: {max_date}\n\n")

    # The public table holds exactly 25 terms per refresh_date (one row per
    # term after the GROUP BY), so the result is the top 25 without a LIMIT.
    query = """
        SELECT
          term,
          refresh_date,
          ARRAY_AGG(STRUCT(rank,week) ORDER BY week DESC LIMIT 1) x
        FROM `bigquery-public-data.google_trends.top_terms`
        WHERE refresh_date = PARSE_DATE('%m/%d/%Y', @max_date)
        GROUP BY term, refresh_date
        ORDER BY (SELECT rank FROM UNNEST(x))
        """
    bq_client = _get_bigquery_client()

    # Setup the query for BigQuery
    query_job_config = _gtrends_job_config(
        [bigquery.ScalarQueryParameter("max_date", "STRING", max_date)]
    )
    # Execute the query and iterate the rows (no pandas needed); rank is
    # the 1-based position in the rank-ordered result.
    job = bq_client.query(query, job_config=query_job_config)
    rows = [
        (row["term"], rank, str(row["refresh_date"]))
        for rank, row in enumerate(
            job.result(timeout=GTRENDS_RESULT_TIMEOUT_SECONDS), start=1
        )
    ]
    return max_date, rows


def _run_date_iso(state_current_date: object) -> str:
    """The run's date (YYYY-MM-DD): state `current_date`, else today in TREND_TIMEZONE."""
    if isinstance(state_current_date, str):
        try:
            return datetime.date.fromisoformat(state_current_date.strip()).isoformat()
        except ValueError:
            pass
    return now_in_tz(config.trend_timezone).date().isoformat()


def _record_gtrends_staleness(state: Any, refresh_iso: str) -> None:
    """Store `gtrends_staleness` + `gtrends_staleness_note`; warn once when stale.

    The warning fires only when the recorded value changes, so a retried or
    repeated gather of the same stale date logs it once per run.
    """
    staleness = gtrends_staleness(
        refresh_iso,
        _run_date_iso(state.get("current_date")),
        config.gtrends_stale_after_days,
    )
    previous = state.get("gtrends_staleness")
    state["gtrends_staleness"] = staleness
    state["gtrends_staleness_note"] = staleness_note(staleness)
    if staleness["stale"] and previous != staleness:
        logging.warning(
            "Google Trends data is stale: newest refresh_date %s is %d days old "
            "(threshold %d days)",
            staleness["refresh_date"],
            staleness["age_days"],
            staleness["threshold_days"],
        )


async def get_daily_gtrends(tool_context: ToolContext) -> str:
    """
    Retrieves the top 25 Google Search Trends (term, rank, refresh_date).

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        str: A markdown-formatted string listing the Google Search Trends and their corresponding
             rank, or an error message if the query fails.
             The table includes columns for 'term', 'rank', and 'refresh_date'.
             Returns 25 rows of results.
    """
    try:
        # Blocking BigQuery I/O runs off the event loop (ADK runs sync tools
        # inline on it); the state writes below stay on the loop.
        max_date, rows = await asyncio.to_thread(_query_daily_gtrends)

        # Update state
        tool_context.state["raw_gtrends"] = [term for term, _, _ in rows]
        # The public table's date as ISO YYYY-MM-DD: the trend digest's cache
        # key (trend_scout/digest.py) and the pick panel's "Google Trends for …".
        refresh_iso = (
            datetime.datetime.strptime(max_date, "%m/%d/%Y").date().isoformat()
        )
        tool_context.state["gtrends_refresh_date"] = refresh_iso
        _record_gtrends_staleness(tool_context.state, refresh_iso)

        return _gtrends_markdown(rows)
    except Exception as e:
        # Let transient failures propagate so ADK 2.0 RetryConfig can retry.
        logging.exception(f"Failed to gather daily trends: {e}")
        raise


def _upload_text_to_gcs(gcs_folder: str, filename: str, content: str) -> str:
    """Upload ``content`` as ``<gcs_folder>/<filename>``; return its gs:// URI (blocking)."""
    storage_client = _get_gcs_client()
    gcs_bucket = config.GCS_BUCKET_NAME
    bucket = storage_client.bucket(gcs_bucket)

    # Per-invocation temp dir: concurrent runs share this process's CWD, so a
    # fixed scratch dir (state["agent_output_dir"] == "trawler_output") + fixed
    # filename collided (issue #104) — one run's rmtree raced another's write.
    # A TemporaryDirectory is unique per call and auto-removed on context exit.
    # The GCS object key uses the file BASENAME so the temp path never leaks in.
    with tempfile.TemporaryDirectory() as td:
        local_file = os.path.join(td, filename)
        Path(local_file).write_text(content)

        gcs_blob_name = f"{gcs_folder}/{filename}"
        blob = bucket.blob(gcs_blob_name)
        blob.upload_from_filename(local_file)

    return f"gs://{gcs_bucket}/{gcs_blob_name}"


async def write_to_file(content: str, tool_context: ToolContext) -> dict:
    """
    Writes the given content to a markdown file. Saves the file to Google Cloud Storage.

    Args:
        content (str): Full markdown content as a string to be saved to disk.
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing the status and the markdown file's Cloud Storage URI (gcs_uri).
    """

    gcs_folder = tool_context.state["gcs_folder"]
    # Blocking GCS upload off the event loop; the state write stays on the loop.
    gcs_uri = await asyncio.to_thread(
        _upload_text_to_gcs, gcs_folder, "selected_trends.txt", content
    )
    tool_context.state["select_trends_markdown_gcs_uri"] = gcs_uri

    # Return a dictionary indicating success, and the artifact_key that was written.
    return {
        "status": "success",
        "gcs_uri": gcs_uri,
    }


def save_search_trends_to_session_state(
    trend_term: str, tool_context: ToolContext
) -> dict:
    """
    Tool to save `trend_term` to the 'target_search_trends' state key.
    Use this tool once the subset of trends have been selected.

    Args:
        trend_term (str): the selected trending search term.
        tool_context: The tool context.

    Returns:
        A status message. An automatically picked term must be one of today's
        gathered trends (state 'raw_gtrends', matched case/space-insensitively
        and saved as gathered) and not flagged sensitive by the classifier
        (state 'trend_flags'); otherwise it is refused and not saved. A term
        already saved is not added twice. A human pick (interactive_trend_pick
        exactly True) is always saved as given.
    """
    if tool_context.state.get("interactive_trend_pick") is not True:
        term_key = norm_term(trend_term)
        raw = tool_context.state.get("raw_gtrends")
        gathered: dict[str, str] = {}
        if isinstance(raw, (list, tuple)):
            for term in raw:
                key = norm_term(term)
                if key:
                    gathered.setdefault(key, term)
        if term_key not in gathered:
            # A paraphrase would also miss the sensitivity lookup below.
            logging.warning("Refused to auto-pick ungathered trend %r", trend_term)
            return {
                "status": "refused",
                "message": (
                    f"'{trend_term}' is not one of today's gathered trends; it was "
                    "not saved. Select a term exactly as it appears in the list."
                ),
            }
        flagged = sensitive_flags(tool_context.state.get("trend_flags"))
        if term_key in flagged:
            reason = flagged[term_key]
            logging.warning(
                "Refused to auto-pick sensitive trend %r (%s)", trend_term, reason
            )
            return {
                "status": "refused",
                "message": (
                    f"'{trend_term}' is flagged sensitive"
                    + (f" ({reason})" if reason else "")
                    + "; it was not saved. Do not select it."
                ),
            }
        trend_term = gathered[term_key]
        saved = tool_context.state.get("target_search_trends") or {}
        if term_key in {norm_term(t) for t in saved.get("target_search_trends", [])}:
            return {"status": "ok", "message": f"'{trend_term}' is already saved."}
    existing_target_search_trends = tool_context.state.get(
        "target_search_trends", {"target_search_trends": []}
    )

    existing_target_search_trends["target_search_trends"].append(trend_term)

    tool_context.state["target_search_trends"] = existing_target_search_trends
    return {"status": "ok"}


async def save_session_state_to_gcs(tool_context: ToolContext) -> dict:
    """
    Writes the session state to JSON. Saves the JSON file to Cloud Storage.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing the status and the json file's Cloud Storage URI (gcs_uri).
    """

    # Snapshot on the loop; serialise + upload (blocking) in a worker thread.
    session_state = tool_context.state.to_dict()
    gcs_folder = session_state["gcs_folder"]

    def _dump_and_upload() -> str:
        content = json.dumps(session_state, indent=4)
        return _upload_text_to_gcs(gcs_folder, "trawler_session_state.json", content)

    gcs_uri = await asyncio.to_thread(_dump_and_upload)

    # Return a dictionary indicating status and the Cloud Storage URI.
    return {
        "status": "success",
        "gcs_uri": gcs_uri,
    }


def _build_trend_insert_sql(
    *,
    table: str,
    unique_id: str,
    trend: str,
    max_date: str,
    current_date: str,
    trawler_gcs: str,
    brand: str,
    target_audience: str,
    target_product: str,
    key_selling_points: str,
    research_gaps: str,
    trend_context: str = "",
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """Build the single-row MERGE for `target_trends_crf` (pure, unit-testable).

    Returns the parameterized SQL plus its bound query parameters. Values are
    passed as `@named` BigQuery query parameters (never string-interpolated) so a
    trend/brand/field containing a quote or apostrophe can't break the statement
    or inject SQL. Only `table` is interpolated — it is a config-derived identifier
    (BigQuery cannot parameterize table names), not user input.

    `research_gaps` mirrors `creative_evals.research_gaps`: an empty string on a
    clean run, or the `collect_degradation_warnings` note(s) when the resilient
    research wrapper exhausted its retries. The `research_gaps` column must already
    exist (additive `ALTER TABLE ... ADD COLUMN research_gaps STRING`).

    `trend_context` is the term's compact research JSON
    (`trend_scout.trend_context.trend_context_for`; "" when nothing parsed),
    forwarded by the CRF worker to the creative run as `trend_scout_context`.
    Its column must exist first too (additive
    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS trend_context STRING`).

    INSERT-only MERGE keyed on `(uuid, target_trend)`: `unique_id` is
    session-derived, so an at-least-once re-run of the tool leaves one row per
    trend instead of duplicating the batch.
    """
    sql = f"""
    MERGE `{table}` T
    USING (
      SELECT
        @unique_id AS uuid,
        @trend AS target_trend,
        PARSE_DATE('%m/%d/%Y', @max_date) AS refresh_date,
        PARSE_DATE('%m/%d/%Y', @current_date) AS trawler_date,
        CURRENT_TIMESTAMP() AS entry_timestamp,
        @trawler_gcs AS trawler_gcs,
        @brand AS brand,
        @target_audience AS target_audience,
        @target_product AS target_product,
        @key_selling_points AS key_selling_point,
        @research_gaps AS research_gaps,
        @trend_context AS trend_context
    ) S
    ON T.uuid = S.uuid AND T.target_trend = S.target_trend
    WHEN NOT MATCHED THEN
      INSERT (uuid,
        -- processed_status, -- omitting will make it NULL
        target_trend,
        refresh_date,
        trawler_date,
        entry_timestamp,
        trawler_gcs,
        brand,
        target_audience,
        target_product,
        key_selling_point,
        research_gaps,
        trend_context)
      VALUES (S.uuid,
        S.target_trend,
        S.refresh_date,
        S.trawler_date,
        S.entry_timestamp,
        S.trawler_gcs,
        S.brand,
        S.target_audience,
        S.target_product,
        S.key_selling_point,
        S.research_gaps,
        S.trend_context);
    """
    params = [
        bigquery.ScalarQueryParameter("unique_id", "STRING", unique_id),
        bigquery.ScalarQueryParameter("trend", "STRING", trend),
        bigquery.ScalarQueryParameter("max_date", "STRING", max_date),
        bigquery.ScalarQueryParameter("current_date", "STRING", current_date),
        bigquery.ScalarQueryParameter("trawler_gcs", "STRING", trawler_gcs),
        bigquery.ScalarQueryParameter("brand", "STRING", brand),
        bigquery.ScalarQueryParameter("target_audience", "STRING", target_audience),
        bigquery.ScalarQueryParameter("target_product", "STRING", target_product),
        bigquery.ScalarQueryParameter(
            "key_selling_points", "STRING", key_selling_points
        ),
        bigquery.ScalarQueryParameter("research_gaps", "STRING", research_gaps),
        bigquery.ScalarQueryParameter("trend_context", "STRING", trend_context),
    ]
    return sql, params


def _trawler_date(state_current_date: object) -> str:
    """The run's date as MM/DD/YYYY for `trawler_date`.

    Uses the per-run `current_date` (YYYY-MM-DD, seeded in TREND_TIMEZONE — the
    date the prompts were given), else today in TREND_TIMEZONE; never the
    server's UTC clock, which can be a day ahead of the run's date.
    """
    run_date = datetime.date.fromisoformat(_run_date_iso(state_current_date))
    return run_date.strftime("%m/%d/%Y")


def _write_trend_rows(state: dict[str, Any], session_id: str) -> dict:
    """Blocking body of write_trends_to_bq, on a state snapshot (worker thread)."""
    bq_client = _get_bigquery_client()

    # refresh_date = the date the trends were GATHERED (get_daily_gtrends seeds
    # gtrends_refresh_date), never the table's latest date at write time: an
    # interactive pause can span a table refresh, which would stamp yesterday's
    # terms with today's date (and seed a wrong trend_detected_on downstream).
    max_date = _gathered_refresh_date(state.get("gtrends_refresh_date"))
    if not max_date:
        max_date = _get_gtrends_max_date()
        logging.warning(
            "gtrends_refresh_date missing; using the table's latest date %s", max_date
        )

    # values to insert. One batch per session: every selected trend shares this
    # session-derived id (not uuid4), so an at-least-once re-run (resumed app)
    # MERGEs onto the same (uuid, target_trend) rows instead of duplicating them.
    unique_id = stable_row_id(session_id)
    current_date = _trawler_date(state.get("current_date"))

    gcs_url_prefix = "https://console.cloud.google.com/storage/browser"
    gcs_folder = state["gcs_folder"]
    # The folder write_to_file / save_session_state_to_gcs actually write into.
    trawler_gcs = f"{gcs_url_prefix}/{config.GCS_BUCKET_NAME}/{gcs_folder}"

    # Degradation note: empty on a clean run, or the collect_degradation_warnings
    # note(s) recorded by record_research_gaps when the resilient research wrapper
    # exhausted its retries. Parity with creative_evals.research_gaps.
    research_gaps = state.get("research_gaps", "")
    table = f"{config.BQ_PROJECT_ID}.{config.BQ_DATASET_ID}.{config.BQ_TABLE_TARGETS}"
    # Per-trend research context for the creative run (best-effort, never raises).
    info_gtrends = state.get("info_gtrends")
    selected_gtrends = state.get("selected_gtrends")
    trend_flags = state.get("trend_flags")

    try:
        # insert a row for each selected target search trend
        target_trends: Any = state.get("target_search_trends")
        for trend in target_trends["target_search_trends"]:
            # write SQL (parameterized — see _build_trend_insert_sql)
            sql_query, query_params = _build_trend_insert_sql(
                table=table,
                unique_id=unique_id,
                trend=trend,
                max_date=max_date,
                current_date=current_date,
                trawler_gcs=trawler_gcs,
                brand=state["brand"],
                target_audience=state["target_audience"],
                target_product=state["target_product"],
                key_selling_points=state["key_selling_points"],
                research_gaps=research_gaps,
                trend_context=trend_context_for(
                    trend, info_gtrends, selected_gtrends, flags=trend_flags
                ),
            )
            # make API request
            job = bq_client.query(
                sql_query,
                job_config=bigquery.QueryJobConfig(query_parameters=query_params),
            )
            # result() raises on a failed job (the except below logs + re-raises).
            job.result()
            logging.info(
                f"DML MERGE job {job.job_id} for trend: `{trend}` completed; added {job.num_dml_affected_rows} rows."
            )
        return {
            "status": "success",
            "trends": ", ".join(target_trends["target_search_trends"]),
        }
    except Exception as e:
        # Let transient failures propagate so ADK 2.0 RetryConfig can retry.
        logging.exception(f"Failed to insert rows to bq: {e}")
        raise


async def write_trends_to_bq(tool_context: ToolContext) -> dict:
    """
    Writes the trends saved in the 'target_search_trends' state key to a BigQuery
    table. Call `save_search_trends_to_session_state` for each trend first.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing a 'status' key ('success' or 'error').
              On success, status is 'success' and includes a 'trends' key with the inserted terms
              On failure, status is 'error' and includes an 'error_message'.
    """
    # The MERGEs (and the fallback max-date lookup) block on BigQuery, so they run off the
    # event loop on a state snapshot; the tool writes no state.
    return await asyncio.to_thread(
        _write_trend_rows, tool_context.state.to_dict(), tool_context.session.id
    )
