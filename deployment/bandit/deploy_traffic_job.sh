#!/usr/bin/env bash
# Deploy (create or update) the bandit synthetic-traffic Cloud Run Job.
#
#   IMAGE_TAG=$(git rev-parse --short HEAD) deployment/bandit/deploy_traffic_job.sh
#
# The api starts executions with per-run env overrides (EXPERIMENT_ID, CONFIG_URI,
# ENDPOINT_ID, EPISODES, HORIZON; see runserver/experiments_jobs.py); this script
# only sets the static BigQuery settings. Build the image first with
# deployment/bandit/cloudbuild.bandit.yaml; IMAGE_TAG is the SHORT_SHA it was built with.
# The job runs the one bandit image (trend-trawler-bandit, whose default entrypoint
# is the CPR model server) with a command override: python -m bandit_traffic.main.
set -euo pipefail

: "${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
: "${IMAGE_TAG:?set IMAGE_TAG (the SHORT_SHA tag pushed by cloudbuild.bandit.yaml)}"
PROJECT="${GOOGLE_CLOUD_PROJECT}"
REGION="${GCP_REGION:-us-central1}"
JOB="${BANDIT_TRAFFIC_JOB:-trend-trawler-bandit-traffic}"
IMAGE="${IMAGE:-us-central1-docker.pkg.dev/${PROJECT}/cpr/trend-trawler-bandit:${IMAGE_TAG}}"
SA="${BANDIT_TRAFFIC_SA:-tt-bandit-traffic-sa@${PROJECT}.iam.gserviceaccount.com}"
BQ_PROJECT_ID="${BQ_PROJECT_ID:-${PROJECT}}"
: "${BQ_DATASET_ID:?set BQ_DATASET_ID}"

ENV_VARS="BQ_PROJECT_ID=${BQ_PROJECT_ID},BQ_DATASET_ID=${BQ_DATASET_ID}"
ENV_VARS+=",BQ_TABLE_BANDIT_EXPERIMENTS=${BQ_TABLE_BANDIT_EXPERIMENTS:-bandit_experiments}"
ENV_VARS+=",BQ_TABLE_BANDIT_EVENTS=${BQ_TABLE_BANDIT_EVENTS:-bandit_events}"
ENV_VARS+=",BQ_TABLE_BANDIT_METRICS=${BQ_TABLE_BANDIT_METRICS:-bandit_episode_metrics}"
ENV_VARS+=",GOOGLE_CLOUD_PROJECT=${PROJECT},GCP_REGION=${REGION}"

# --task-timeout 3600 bounds a run at ~2.3M rounds (~650 rounds/s); the api caps a
# traffic run at runserver.experiments.MAX_TRAFFIC_ROUNDS (1.5M) to fit it, so raise
# both together. At the timeout the job gets SIGTERM, flushes its events, exits 143.

gcloud run jobs deploy "${JOB}" \
  --project "${PROJECT}" \
  --image "${IMAGE}" \
  --command python \
  --args=-m,bandit_traffic.main \
  --region "${REGION}" \
  --service-account "${SA}" \
  --task-timeout 3600 \
  --max-retries 0 \
  --tasks 1 \
  --memory 2Gi \
  --cpu 2 \
  --set-env-vars "${ENV_VARS}" \
  --labels app=trend-trawler,component=bandit-traffic
