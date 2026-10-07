"""BigQuery persistence tools: creative rows and evaluation summaries."""

import datetime
import logging
from zoneinfo import ZoneInfo

from google.adk.tools import ToolContext
from google.cloud import bigquery

from agent_common.clients import get_bigquery_client
from agent_common.idempotency import stable_row_id
from creative_eval.dimensions import dimension_labels_csv

from .config import config

# Shared lazy getter (agent_common.clients), bound to the historical private name
# so call sites + test monkeypatch points are unchanged.
_get_bigquery_client = get_bigquery_client


def build_eval_bq_row(
    *,
    report: dict,
    eval_uuid: str,
    creative_uuid: str,
    now_datetime: str,
    target_trend: str,
    brand: str,
    target_product: str,
    eval_report_gcs_uri: str,
) -> dict:
    """Flatten a CreativeEvaluationReport dict into one BigQuery row.

    Pure (no client, no wall-clock) so it is unit-testable. Numeric fields are
    coerced because the judge's JSON round-trip can hand back stringified numbers.
    """
    summary = report.get("summary", {})
    weakest = summary.get("weakest_dimensions") or []
    warnings = report.get("warnings") or []
    return {
        "uuid": eval_uuid,
        "creative_uuid": creative_uuid,
        "datetime": now_datetime,
        "target_trend": target_trend,
        "brand": brand,
        "target_product": target_product,
        "overall_pass_rate": float(summary.get("overall_pass_rate", 0.0)),
        "total_ad_copies": int(summary.get("total_ad_copies", 0)),
        "ad_copies_passed": int(summary.get("ad_copies_passed", 0)),
        "avg_ad_copy_score": float(summary.get("avg_ad_copy_score", 0.0)),
        "total_visual_concepts": int(summary.get("total_visual_concepts", 0)),
        "visual_concepts_passed": int(summary.get("visual_concepts_passed", 0)),
        "avg_visual_score": float(summary.get("avg_visual_score", 0.0)),
        "weakest_dimensions": ",".join(weakest),
        # Same dimensions, readable ("Trend connection, Copy quality") for BI/UI.
        "weakest_dimension_labels": dimension_labels_csv(weakest),
        "eval_report_gcs_uri": eval_report_gcs_uri,
        # Degradation notes (research retries exhausted, etc.) surfaced from the
        # eval report's structured `warnings`. Empty string when research was clean.
        "research_gaps": " | ".join(warnings),
    }


# BigQuery types of the `creative_evals` columns (mirrors the `bq mk` schema in
# deployment/README.md). build_eval_bq_row stays the single source of *which* columns are
# written; this map only types them so values can be bound as query parameters.
EVAL_COLUMN_TYPES = {
    "uuid": "STRING",
    "creative_uuid": "STRING",
    "datetime": "DATETIME",
    "target_trend": "STRING",
    "brand": "STRING",
    "target_product": "STRING",
    "overall_pass_rate": "FLOAT64",
    "total_ad_copies": "INT64",
    "ad_copies_passed": "INT64",
    "avg_ad_copy_score": "FLOAT64",
    "total_visual_concepts": "INT64",
    "visual_concepts_passed": "INT64",
    "avg_visual_score": "FLOAT64",
    "weakest_dimensions": "STRING",
    "weakest_dimension_labels": "STRING",
    "eval_report_gcs_uri": "STRING",
    "research_gaps": "STRING",
}


def _build_eval_merge_sql(
    table: str, row: dict
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """Build the INSERT-only MERGE for one eval row (pure, unit-testable).

    Columns come from the row dict's keys; every value is bound as a typed
    `@named` parameter (None -> typed NULL), never interpolated. Keyed on
    ``uuid``, so a repeat write of the same session-derived id is a no-op.
    Raises KeyError for a column missing from EVAL_COLUMN_TYPES.
    """
    cols = list(row)
    params = []
    for col in cols:
        bq_type, value = EVAL_COLUMN_TYPES[col], row[col]
        if bq_type == "DATETIME" and isinstance(value, str):
            # The client round-trips DATETIME params through a strict ISO parser;
            # bind a datetime (not the row's "YYYY-MM-DD HH:MM:SS" string).
            value = datetime.datetime.fromisoformat(value)
        params.append(bigquery.ScalarQueryParameter(col, bq_type, value))
    select_list = ",\n                ".join(f"@{c} AS {c}" for c in cols)
    sql = f"""
        MERGE `{table}` T
        USING (
            SELECT
                {select_list}
        ) S
        ON T.uuid = S.uuid
        WHEN NOT MATCHED THEN
            INSERT ({", ".join(cols)})
            VALUES ({", ".join(f"S.{c}" for c in cols)});
        """
    return sql, params


def write_trends_to_bq(tool_context: ToolContext) -> dict:
    """Writes this run's creative-results row (campaign metadata + GCS folder) to BigQuery.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: On success, ``{"status": "success", "trend": <target_search_trends>}``.
              Raises on failure (so transient BigQuery errors can be retried).
    """
    bq_client = _get_bigquery_client()

    # values to insert. The row key is derived from the session (not uuid4) so an
    # at-least-once re-run (resumed app, CRF retry) maps to the same row, and the
    # MERGE below inserts it only once. 8 chars: CRF joins on creative_uuid.
    target_trend = tool_context.state["target_search_trends"]
    unique_id = stable_row_id(tool_context.session.id, target_trend)
    tool_context.state["creative_row_uuid"] = unique_id
    gcs_url_prefix = "https://console.cloud.google.com/storage/browser"
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_dir = tool_context.state["agent_output_dir"]
    creative_gcs = f"{gcs_url_prefix}/{config.GCS_BUCKET_NAME}/{gcs_folder}/{gcs_dir}"

    try:
        # write SQL — parameterized (@named) so a trend/brand/field containing a
        # quote or apostrophe can't break the statement or inject SQL. The table
        # name is a config-derived identifier (not parameterizable), not user input.
        # INSERT-only MERGE on uuid: a repeat call for the same session is a no-op.
        sql_query = f"""
        MERGE
            `{config.BQ_PROJECT_ID}.{config.BQ_DATASET_ID}.{config.BQ_TABLE_CREATIVES}` T
        USING (
            SELECT
                @unique_id AS uuid,
                @target_trend AS target_trend,
                CURRENT_DATETIME('America/New_York') AS datetime,
                @creative_gcs AS creative_gcs,
                @brand AS brand,
                @target_audience AS target_audience,
                @target_product AS target_product,
                @key_selling_points AS key_selling_point
        ) S
        ON T.uuid = S.uuid
        WHEN NOT MATCHED THEN
            INSERT (uuid,
                target_trend,
                datetime,
                creative_gcs,
                brand,
                target_audience,
                target_product,
                key_selling_point)
            VALUES (S.uuid,
                S.target_trend,
                S.datetime,
                S.creative_gcs,
                S.brand,
                S.target_audience,
                S.target_product,
                S.key_selling_point);
        """
        query_params = [
            bigquery.ScalarQueryParameter("unique_id", "STRING", unique_id),
            bigquery.ScalarQueryParameter("target_trend", "STRING", target_trend),
            bigquery.ScalarQueryParameter("creative_gcs", "STRING", creative_gcs),
            bigquery.ScalarQueryParameter(
                "brand", "STRING", tool_context.state["brand"]
            ),
            bigquery.ScalarQueryParameter(
                "target_audience", "STRING", tool_context.state["target_audience"]
            ),
            bigquery.ScalarQueryParameter(
                "target_product", "STRING", tool_context.state["target_product"]
            ),
            bigquery.ScalarQueryParameter(
                "key_selling_points",
                "STRING",
                tool_context.state["key_selling_points"],
            ),
        ]
        # make API request
        job = bq_client.query(
            sql_query,
            job_config=bigquery.QueryJobConfig(query_parameters=query_params),
        )
        job.result()  # wait for job to complete
        if job.errors:
            logging.error(
                f"DML MERGE job for trend: '{target_trend}' failed: {job.errors}"
            )
            raise RuntimeError(f"BigQuery insert returned errors: {job.errors}")
        else:
            logging.info(
                f"DML MERGE job {job.job_id} for trend: '{target_trend}' completed; added {job.num_dml_affected_rows} rows."
            )
        return {
            "status": "success",
            "trend": target_trend,
        }
    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"Failed to insert row to bq: {e}")
        raise


def write_eval_report_to_bq(tool_context: ToolContext) -> dict:
    """Write a one-row creative-evaluation summary to BigQuery.

    Reads the report the evaluator stored in state, flattens it via
    build_eval_bq_row, and MERGEs it into the ``BQ_TABLE_EVALS`` table. The row
    foreign-keys to the trend_creatives row via ``creative_row_uuid`` and links
    to the full per-dimension JSON already saved in GCS.

    Call after `save_eval_report_to_gcs` and `write_trends_to_bq` (it links to both).
    """
    report = tool_context.state.get("creative_evaluation_report")
    if not report:
        return {
            "status": "error",
            "message": "No creative_evaluation_report found in session state.",
        }

    now_dt = (
        datetime.datetime.now(ZoneInfo("America/New_York"))
        .replace(tzinfo=None)
        .isoformat(sep=" ", timespec="seconds")
    )

    row = build_eval_bq_row(
        report=report,
        # Session-derived (not uuid4) so an at-least-once re-run hits the same row.
        eval_uuid=stable_row_id(tool_context.session.id, "eval"),
        creative_uuid=tool_context.state.get("creative_row_uuid", ""),
        now_datetime=now_dt,
        target_trend=tool_context.state.get("target_search_trends", ""),
        brand=tool_context.state.get("brand", ""),
        target_product=tool_context.state.get("target_product", ""),
        eval_report_gcs_uri=tool_context.state.get("eval_report_gcs_uri", ""),
    )

    table_id = f"{config.BQ_PROJECT_ID}.{config.BQ_DATASET_ID}.{config.BQ_TABLE_EVALS}"
    try:
        bq_client = _get_bigquery_client()
        # DML MERGE rather than streaming insert_rows_json, which can't dedupe.
        sql_query, query_params = _build_eval_merge_sql(table_id, row)
        job = bq_client.query(
            sql_query,
            job_config=bigquery.QueryJobConfig(query_parameters=query_params),
        )
        job.result()  # wait for job to complete
        if job.errors:
            logging.error(f"Eval-row MERGE into {table_id} failed: {job.errors}")
            raise RuntimeError(f"BigQuery insert returned errors: {job.errors}")
        logging.info(
            f"DML MERGE job {job.job_id} for eval row {row['uuid']} into {table_id}"
            f" completed; added {job.num_dml_affected_rows} rows."
        )
        # The workflow's final step: runserver's auto-continue treats the run as
        # finished only once this is set (not at eval_report_gcs_uri, which is
        # written earlier), so an empty root turn before this write is re-prompted.
        tool_context.state["eval_bq_row_uuid"] = row["uuid"]
        return {"status": "success", "eval_uuid": row["uuid"]}
    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"Failed to insert eval row to bq: {e}")
        raise
