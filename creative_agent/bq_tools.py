"""BigQuery persistence tools: creative rows and evaluation summaries."""

import uuid
import logging
import datetime
from zoneinfo import ZoneInfo

from google.cloud import bigquery
from google.adk.tools import ToolContext

from .config import config


def _get_bigquery_client() -> bigquery.Client:
    """Get a configured BigQuery client."""
    return bigquery.Client(project=config.BQ_PROJECT_ID)


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
        "eval_report_gcs_uri": eval_report_gcs_uri,
        # Degradation notes (research retries exhausted, etc.) surfaced from the
        # eval report's structured `warnings`. Empty string when research was clean.
        "research_gaps": " | ".join(warnings),
    }


def write_trends_to_bq(tool_context: ToolContext) -> dict:
    """
    Writes selected trends to a BigQuery Table.

    Args:
        tool_context (ToolContext): The tool context.

    Returns:
        dict: A dictionary containing a 'status' key ('success' or 'error').
              On success, status is 'success' and includes a 'trends' key with the inserted terms
              On failure, status is 'error' and includes an 'error_message'.
    """
    bq_client = _get_bigquery_client()

    # values to insert
    unique_id = f"{str(uuid.uuid4())[:8]}"
    tool_context.state["creative_row_uuid"] = unique_id
    gcs_url_prefix = "https://console.cloud.google.com/storage/browser"
    gcs_folder = tool_context.state["gcs_folder"]
    gcs_dir = tool_context.state["agent_output_dir"]
    creative_gcs = f"{gcs_url_prefix}/{config.GCS_BUCKET_NAME}/{gcs_folder}/{gcs_dir}"

    try:
        # insert a row for the target search trend
        target_trend = tool_context.state["target_search_trends"]
        # write SQL — parameterized (@named) so a trend/brand/field containing a
        # quote or apostrophe can't break the statement or inject SQL. The table
        # name is a config-derived identifier (not parameterizable), not user input.
        sql_query = f"""
        INSERT INTO
            `{config.BQ_PROJECT_ID}.{config.BQ_DATASET_ID}.{config.BQ_TABLE_CREATIVES}` (uuid,
            target_trend,
            datetime,
            creative_gcs,
            brand,
            target_audience,
            target_product,
            key_selling_point)
        VALUES
        (
            @unique_id,
            @target_trend,
            CURRENT_DATETIME('America/New_York'),
            @creative_gcs,
            @brand,
            @target_audience,
            @target_product,
            @key_selling_points
        );
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
                f"DML INSERT job for trend: '{target_trend}' failed: {job.errors}"
            )
            raise RuntimeError(f"BigQuery insert returned errors: {job.errors}")
        else:
            logging.info(
                f"DML INSERT job {job.job_id} for trend: '{target_trend}' completed; added {job.num_dml_affected_rows} rows."
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
    build_eval_bq_row, and streams it to the ``BQ_TABLE_EVALS`` table. The row
    foreign-keys to the trend_creatives row via ``creative_row_uuid`` and links
    to the full per-dimension JSON already saved in GCS.
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
        eval_uuid=str(uuid.uuid4())[:8],
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
        errors = bq_client.insert_rows_json(table_id, [row])
        if errors:
            logging.error(f"Eval-row insert into {table_id} failed: {errors}")
            raise RuntimeError(f"BigQuery insert returned errors: {errors}")
        logging.info(f"Inserted eval summary row {row['uuid']} into {table_id}.")
        return {"status": "success", "eval_uuid": row["uuid"]}
    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"Failed to insert eval row to bq: {e}")
        raise
