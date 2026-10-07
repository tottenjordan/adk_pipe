#!/usr/bin/env bash
# Create the Trend Trawler BigQuery dataset and its tables. Idempotent: anything
# that already exists is left untouched (use the ALTER migrations in
# deployment/README.md to add columns to an older table).
#
# Usage (from the repo root, with .env populated):
#   set -a && source .env && set +a
#   bash deployment/create_bq_tables.sh
#
# Schemas mirror the writers: trend_scout/tools.py (targets), creative_agent/bq_tools.py
# (creatives + EVAL_COLUMN_TYPES for evals) and the CRF lock/reaper columns in
# cloud_functions/creative_fanout/main.py. The bandit_* tables follow
# docs/bandit/contracts.md §3 (JSON payloads are STRING columns); creative_ratings
# mirrors RATING_COLUMN_TYPES in runserver/ratings_store.py.
set -euo pipefail

: "${BQ_PROJECT_ID:?set BQ_PROJECT_ID}"
: "${BQ_DATASET_ID:?set BQ_DATASET_ID}"
: "${BQ_TABLE_TARGETS:?set BQ_TABLE_TARGETS}"
: "${BQ_TABLE_CREATIVES:?set BQ_TABLE_CREATIVES}"
: "${BQ_TABLE_EVALS:?set BQ_TABLE_EVALS}"
BQ_LOCATION="${BQ_LOCATION:-US}"
# Bandit experiment tables (runserver/experiments_store.py + the traffic job).
BQ_TABLE_BANDIT_EXPERIMENTS="${BQ_TABLE_BANDIT_EXPERIMENTS:-bandit_experiments}"
BQ_TABLE_BANDIT_EVENTS="${BQ_TABLE_BANDIT_EVENTS:-bandit_events}"
BQ_TABLE_BANDIT_METRICS="${BQ_TABLE_BANDIT_METRICS:-bandit_episode_metrics}"
# Human creative ratings for judge calibration (runserver/ratings_store.py).
BQ_TABLE_RATINGS="${BQ_TABLE_RATINGS:-creative_ratings}"

DATASET="${BQ_PROJECT_ID}:${BQ_DATASET_ID}"

if bq show --dataset "${DATASET}" >/dev/null 2>&1; then
  echo "dataset ${DATASET} exists"
else
  bq --location="${BQ_LOCATION}" mk --dataset "${DATASET}"
fi

make_table() {
  # Extra args (partitioning/clustering flags) go before the table name.
  local table="$1" schema="$2"
  shift 2
  if bq show "${DATASET}.${table}" >/dev/null 2>&1; then
    echo "table ${DATASET}.${table} exists"
  else
    bq mk -t "$@" "${DATASET}.${table}" "${schema}"
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
  uuid:STRING,creative_uuid:STRING,datetime:DATETIME,target_trend:STRING,brand:STRING,target_product:STRING,overall_pass_rate:FLOAT,total_ad_copies:INTEGER,ad_copies_passed:INTEGER,avg_ad_copy_score:FLOAT,total_visual_concepts:INTEGER,visual_concepts_passed:INTEGER,avg_visual_score:FLOAT,weakest_dimensions:STRING,eval_report_gcs_uri:STRING,research_gaps:STRING,weakest_dimension_labels:STRING,gates_pass_rate:FLOAT

# Bandit experiments, one row per experiment (the api MERGE-upserts on experiment_id).
make_table "${BQ_TABLE_BANDIT_EXPERIMENTS}" \
  experiment_id:STRING,user_id:STRING,session_id:STRING,app_name:STRING,created_at:TIMESTAMP,updated_at:TIMESTAMP,status:STRING,scenario:STRING,ctr_mode:STRING,reward_mode:STRING,arms:STRING,config_uri:STRING,model_resource:STRING,endpoint_id:STRING,deployed_model_id:STRING,ttl_expires_at:TIMESTAMP,stopped_at:TIMESTAMP,traffic_execution:STRING,progress:STRING,error:STRING,scenario_overrides:STRING,deploy_lease_until:TIMESTAMP,deploy_lease_owner:STRING,policy_discount:FLOAT,traffic_runs:STRING

# Bandit events, one row per endpoint-policy round (traffic job; insertId = request_id).
make_table "${BQ_TABLE_BANDIT_EVENTS}" \
  experiment_id:STRING,episode:INTEGER,round:INTEGER,batch:INTEGER,request_id:STRING,ts:TIMESTAMP,policy:STRING,segment:STRING,context:STRING,arm:STRING,propensity:FLOAT,reward:FLOAT,clicked:INTEGER,dwell_s:FLOAT,p_chosen:FLOAT,p_optimal:FLOAT,optimal_arm:STRING,regret:FLOAT,model_version:STRING,latency_ms:FLOAT,traffic_run:INTEGER \
  --time_partitioning_field ts --time_partitioning_type DAY --clustering_fields experiment_id

# Bandit episode metrics, one row per (episode, policy) (traffic job).
make_table "${BQ_TABLE_BANDIT_METRICS}" \
  experiment_id:STRING,episode:INTEGER,policy:STRING,horizon:INTEGER,total_reward:FLOAT,total_clicks:INTEGER,cumulative_regret:FLOAT,pct_optimal:FLOAT,steps_to_converge:INTEGER,curve:STRING,arm_share:STRING,per_segment:STRING,arm_stats:STRING,created_at:TIMESTAMP,traffic_run:INTEGER,shift_response:STRING,regimes:STRING

# Human creative ratings, one row per (session, creative, user) (the api MERGE-upserts
# on rating_id); judge_* snapshot the LLM judge's verdict for calibration.
make_table "${BQ_TABLE_RATINGS}" \
  rating_id:STRING,session_id:STRING,app_name:STRING,creative_key:STRING,kind:STRING,user_id:STRING,verdict:STRING,score:INTEGER,note:STRING,judge_overall:FLOAT,judge_passed:BOOLEAN,judge_gates_passed:BOOLEAN,judge_model:STRING,created_at:TIMESTAMP,updated_at:TIMESTAMP \
  --clustering_fields user_id,session_id
