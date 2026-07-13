#!/bin/sh
# Container entrypoint for the trend-trawler-api Cloud Run backend.
# Runs `adk api_server agents` and appends --session_service_uri ONLY when
# SESSION_SERVICE_URI is set and non-empty (opt-in persistent sessions).
# Set ADK_DRYRUN=1 to print the argv instead of exec'ing (used by tests).
set -eu

set -- adk api_server agents --host 0.0.0.0 --port "${PORT:-8080}"

if [ -n "${SESSION_SERVICE_URI:-}" ]; then
  set -- "$@" --session_service_uri "${SESSION_SERVICE_URI}"
fi

if [ -n "${ADK_DRYRUN:-}" ]; then
  echo "$@"
  exit 0
fi

exec uv run "$@"
