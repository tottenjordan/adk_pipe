#!/usr/bin/env bash
# Run a command in a clean, explicit environment built only from a deploy profile.
#
#   deployment/cloudbuild_env.sh <profile.env> <expected project> -- <command> [args...]
#
# Used by deployment/cloudbuild.agents.yaml so an Agent Engine deploy never sees
# inherited state: the command starts from `env -i` (only PATH and HOME kept), then
# the profile is sourced (`set -a`), because deploy_agent.py bakes the process
# environment into the pickled agents (ENV_VAR_DICT, CRITIC_FALLBACK_MODEL,
# MODEL_ARMOR_*, MODEL_REQUEST_TIMEOUT_SECONDS ...). Refuses to run when the
# profile targets another project than the build's, or sets an engine ID / the
# legacy BUCKET / GCS_BUCKET_NAME vars.
set -euo pipefail

if [[ $# -lt 4 || "$3" != "--" ]]; then
  echo "usage: $0 <profile.env> <expected project> -- <command> [args...]" >&2
  exit 2
fi
profile="$1"
expected_project="$2"
shift 3

if [[ ! -f "$profile" ]]; then
  echo "deploy profile not found: $profile" >&2
  exit 2
fi

exec env -i PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}" HOME="${HOME:-/builder/home}" \
  /bin/bash -c '
    set -euo pipefail
    profile="$1"; expected_project="$2"; shift 2
    set -a
    # shellcheck disable=SC1090
    . "$profile"
    set +a
    if [[ "${GOOGLE_CLOUD_PROJECT:-}" != "$expected_project" ]]; then
      echo "profile $profile targets project ${GOOGLE_CLOUD_PROJECT:-<unset>}, not $expected_project" >&2
      exit 2
    fi
    stale="$(env | grep -oE "^([A-Z_]+_AGENT_ENGINE_ID|BUCKET|GCS_BUCKET_NAME)=" || true)"
    if [[ -n "$stale" ]]; then
      echo "refusing: profile $profile sets" $stale "(engine IDs / legacy bucket vars never belong in a deploy profile)" >&2
      exit 2
    fi
    exec "$@"
  ' _ "$profile" "$expected_project" "$@"
