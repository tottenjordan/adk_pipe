#!/usr/bin/env bash
# Deploy (create or update) the bandit synthetic-traffic Cloud Run Job.
#
#   deployment/bandit/deploy_traffic_job.sh <repo>/trend-trawler-bandit@sha256:<digest>
#   IMAGE=<repo>/trend-trawler-bandit@sha256:<digest> deployment/bandit/deploy_traffic_job.sh
#
# Deploy by digest: the BANDIT_IMAGE=…@sha256:… line cloudbuild.bandit.yaml prints,
# the same value as the api's BANDIT_SERVING_IMAGE, so the job and the endpoint
# provably run the same build. The first argument wins over IMAGE. IMAGE_TAG (the
# SHORT_SHA tag) is only a fallback and warns: a tag can be moved after the build.
#
# The api starts executions with per-run env overrides (EXPERIMENT_ID, CONFIG_URI,
# ENDPOINT_ID, EPISODES, HORIZON; see runserver/experiments_jobs.py); this script
# only sets the static BigQuery settings. Build the image first with
# deployment/bandit/cloudbuild.bandit.yaml.
# The job runs the one bandit image (trend-trawler-bandit, whose default entrypoint
# is the CPR model server) with a command override: python -m bandit_traffic.main.
set -euo pipefail

: "${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
PROJECT="${GOOGLE_CLOUD_PROJECT}"
REGION="${GCP_REGION:-us-central1}"
JOB="${BANDIT_TRAFFIC_JOB:-trend-trawler-bandit-traffic}"
IMAGE="${1:-${IMAGE:-}}"
if [[ -z "${IMAGE}" ]]; then
  : "${IMAGE_TAG:?set IMAGE (or pass it) to the bandit image digest, …/trend-trawler-bandit@sha256:…}"
  IMAGE="us-central1-docker.pkg.dev/${PROJECT}/cpr/trend-trawler-bandit:${IMAGE_TAG}"
fi
if [[ ! "${IMAGE}" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "WARNING: deploying ${IMAGE} by tag, not by digest; the job may not run the" \
    "endpoint's build. Pass the BANDIT_IMAGE=…@sha256:… digest instead." >&2
fi
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
