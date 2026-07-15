#!/bin/sh
# Container entrypoint for the trend-trawler-api Cloud Run backend.
# Serves deployment/async_app.py (the ADK FastAPI app + our async-job /runs
# router) under uvicorn. SESSION_SERVICE_URI is no longer a CLI flag — it is read
# inside async_app.py by get_fast_api_app (opt-in persistent sessions), so this
# script only surfaces it for visibility and keeps it in the environment uvicorn
# inherits.
# Set ADK_DRYRUN=1 to print the argv instead of exec'ing (used by tests).
set -eu

set -- uv run uvicorn deployment.async_app:app --host 0.0.0.0 --port "${PORT:-8080}"

if [ -n "${SESSION_SERVICE_URI:-}" ]; then
  echo "SESSION_SERVICE_URI is set; async_app will use persistent sessions." >&2
fi

if [ -n "${ADK_DRYRUN:-}" ]; then
  echo "$@"
  exit 0
fi

exec "$@"
