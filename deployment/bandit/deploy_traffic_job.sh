#!/usr/bin/env bash
# Deploy (create or update) the bandit synthetic-traffic Cloud Run Job.
#
#   IMAGE_TAG=$(git rev-parse --short HEAD) deployment/bandit/deploy_traffic_job.sh
#
# The api starts executions with per-run env overrides (EXPERIMENT_ID, CONFIG_URI,
# ENDPOINT_ID, EPISODES, HORIZON; see runserver/experiments_jobs.py); this script
# only sets the static BigQuery settings. Build the image first with
# deployment/bandit/cloudbuild.traffic.yaml.
set -euo pipefail

: "${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
: "${IMAGE_TAG:?set IMAGE_TAG (the image tag pushed by cloudbuild.traffic.yaml)}"
PROJECT="${GOOGLE_CLOUD_PROJECT}"
REGION="${GCP_REGION:-us-central1}"
JOB="${BANDIT_TRAFFIC_JOB:-trend-trawler-bandit-traffic}"
IMAGE="${IMAGE:-us-central1-docker.pkg.dev/${PROJECT}/cpr/trend-trawler-bandit-traffic:${IMAGE_TAG}}"
SA="${BANDIT_TRAFFIC_SA:-tt-bandit-traffic-sa@${PROJECT}.iam.gserviceaccount.com}"
BQ_PROJECT_ID="${BQ_PROJECT_ID:-${PROJECT}}"
: "${BQ_DATASET_ID:?set BQ_DATASET_ID}"

ENV_VARS="BQ_PROJECT_ID=${BQ_PROJECT_ID},BQ_DATASET_ID=${BQ_DATASET_ID}"
ENV_VARS+=",BQ_TABLE_BANDIT_EXPERIMENTS=${BQ_TABLE_BANDIT_EXPERIMENTS:-bandit_experiments}"
ENV_VARS+=",BQ_TABLE_BANDIT_EVENTS=${BQ_TABLE_BANDIT_EVENTS:-bandit_events}"
ENV_VARS+=",BQ_TABLE_BANDIT_METRICS=${BQ_TABLE_BANDIT_METRICS:-bandit_episode_metrics}"
ENV_VARS+=",GOOGLE_CLOUD_PROJECT=${PROJECT},GCP_REGION=${REGION}"

gcloud run jobs deploy "${JOB}" \
  --project "${PROJECT}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --service-account "${SA}" \
  --task-timeout 3600 \
  --max-retries 0 \
  --tasks 1 \
  --memory 2Gi \
  --cpu 2 \
  --set-env-vars "${ENV_VARS}" \
  --labels app=trend-trawler,component=bandit-traffic
