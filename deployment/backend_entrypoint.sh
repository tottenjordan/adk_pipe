#!/bin/sh
# Container entrypoint for the trend-trawler-api Cloud Run backend.
# Serves deployment/async_app.py (the ADK FastAPI app + our async-job /runs
# router) under uvicorn. SESSION_SERVICE_URI is no longer a CLI flag — it is read
# inside async_app.py by get_fast_api_app (opt-in persistent sessions), so this
# script only surfaces it for visibility and keeps it in the environment uvicorn
# inherits.
# Set ADK_DRYRUN=1 to print the argv instead of exec'ing (used by tests).
set -eu

# Shutdown budget (Cloud Run SIGKILLs 10 s after SIGTERM): uvicorn waits at most
# 6 s for open connections (default: no limit), then the app lifespan cancels the
# detached /runs + /variants tasks and waits <= 3 s for their interrupted markers,
# leaving ~1 s for ADK's runner cleanup. Keep in sync with runserver/shutdown.py
# (UVICORN_GRACEFUL_SECONDS, SHUTDOWN_DRAIN_SECONDS).
set -- uv run uvicorn deployment.async_app:app --host 0.0.0.0 --port "${PORT:-8080}" \
  --timeout-graceful-shutdown 6

if [ -n "${SESSION_SERVICE_URI:-}" ]; then
  echo "SESSION_SERVICE_URI is set; async_app will use persistent sessions." >&2
fi

if [ -n "${ADK_DRYRUN:-}" ]; then
  echo "$@"
  exit 0
fi

exec "$@"
