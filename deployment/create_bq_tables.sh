#!/usr/bin/env bash
# Create the Trend Trawler BigQuery dataset and its three tables. Idempotent: anything
# that already exists is left untouched (use the ALTER migrations in
# deployment/README.md to add columns to an older table).
#
# Usage (from the repo root, with .env populated):
#   set -a && source .env && set +a
#   bash deployment/create_bq_tables.sh
#
# Schemas mirror the writers: trend_scout/tools.py (targets), creative_agent/bq_tools.py
# (creatives + EVAL_COLUMN_TYPES for evals) and the CRF lock/reaper columns in
# cloud_functions/creative_fanout/main.py.
set -euo pipefail

: "${BQ_PROJECT_ID:?set BQ_PROJECT_ID}"
: "${BQ_DATASET_ID:?set BQ_DATASET_ID}"
: "${BQ_TABLE_TARGETS:?set BQ_TABLE_TARGETS}"
: "${BQ_TABLE_CREATIVES:?set BQ_TABLE_CREATIVES}"
: "${BQ_TABLE_EVALS:?set BQ_TABLE_EVALS}"
BQ_LOCATION="${BQ_LOCATION:-US}"

DATASET="${BQ_PROJECT_ID}:${BQ_DATASET_ID}"

if bq show --dataset "${DATASET}" >/dev/null 2>&1; then
  echo "dataset ${DATASET} exists"
else
  bq --location="${BQ_LOCATION}" mk --dataset "${DATASET}"
fi

make_table() {
  local table="$1" schema="$2"
  if bq show "${DATASET}.${table}" >/dev/null 2>&1; then
    echo "table ${DATASET}.${table} exists"
  else
    bq mk -t "${DATASET}.${table}" "${schema}"
  fi
}

# Selected search trends (trend_scout writes; the CRF orchestrator/worker read + lock).
make_table "${BQ_TABLE_TARGETS}" \
  uuid:STRING,processed_status:STRING,target_trend:STRING,refresh_date:DATE,trawler_date:DATE,entry_timestamp:TIMESTAMP,trawler_gcs:STRING,brand:STRING,target_audience:STRING,target_product:STRING,key_selling_point:STRING,research_gaps:STRING,processing_started_at:TIMESTAMP,processing_attempts:INTEGER

# Creative results, one row per creative run.
make_table "${BQ_TABLE_CREATIVES}" \
  uuid:STRING,target_trend:STRING,datetime:DATETIME,creative_gcs:STRING,brand:STRING,target_audience:STRING,target_product:STRING,key_selling_point:STRING

# Creative evaluation summaries, one row per run (creative_uuid joins trend_creatives.uuid).
make_table "${BQ_TABLE_EVALS}" \
  uuid:STRING,creative_uuid:STRING,datetime:DATETIME,target_trend:STRING,brand:STRING,target_product:STRING,overall_pass_rate:FLOAT,total_ad_copies:INTEGER,ad_copies_passed:INTEGER,avg_ad_copy_score:FLOAT,total_visual_concepts:INTEGER,visual_concepts_passed:INTEGER,avg_visual_score:FLOAT,weakest_dimensions:STRING,eval_report_gcs_uri:STRING,research_gaps:STRING,weakest_dimension_labels:STRING
