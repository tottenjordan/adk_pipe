#!/usr/bin/env bash
# Trend-trawler-only retention: Artifact Registry cleanup policies + bucket lifecycles.
#
#   deployment/cleanup/apply.sh            # --preview (default): read-only report
#   deployment/cleanup/apply.sh --apply    # set the policies (asks nothing; read the preview first)
#
# --preview makes NO mutating calls (only describe/list/ls); it does not even call
# `set-cleanup-policies --dry-run`, which still writes a policy to the repo. It simulates
# each policy against the current listings, prints the current repo policies / bucket
# lifecycles, and fails (exit 3) when a digest Cloud Run is serving (or the api would
# deploy for bandit experiments) is a deletion candidate.
#
# --apply refuses when a repo already has cleanup policies, or a bucket a lifecycle, that
# differ from the file here: set-cleanup-policies and --lifecycle-file both REPLACE the
# whole configuration, so merge by hand first. Docs: deployment/README.md -> Retention.
set -euo pipefail

MODE="${1:---preview}"
case "$MODE" in
  --preview | --apply) ;;
  *)
    echo "usage: $0 [--preview|--apply]" >&2
    exit 2
    ;;
esac

PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION=us-central1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=(python3 -I "$HERE/retention.py")
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# repo -> policy file
AR_REPOS=(cloud-run-source-deploy cpr)
ar_file() { echo "$HERE/ar-$1.json"; }
# bucket -> lifecycle file
BUCKETS=(trend-trawler-deploy-ae run-sources-hybrid-vertex-us-central1)
gcs_file() {
  case "$1" in
    trend-trawler-deploy-ae) echo "$HERE/gcs-trend-trawler-deploy-ae.json" ;;
    run-sources-hybrid-vertex-us-central1) echo "$HERE/gcs-run-sources.json" ;;
  esac
}

echo "Project: $PROJECT   Region: $REGION   Mode: $MODE"

# --- digests that must survive ------------------------------------------------------
resolve_digest() { # image ref (tag or digest) -> sha256:...
  local ref="$1"
  if [[ "$ref" == *@sha256:* ]]; then
    echo "${ref##*@}"
  else
    gcloud artifacts docker images describe "$ref" --format='value(image_summary.digest)'
  fi
}

live_digests() {
  local svc rev job img
  for svc in trend-trawler-api trend-trawler-web trend-trawler-share creative-trawler-crf creative-worker-crf; do
    # Every revision receiving traffic (pinned rollouts can split), plus the latest ready.
    for rev in $(gcloud run services describe "$svc" --region "$REGION" --project "$PROJECT" \
      --format='value(status.traffic[].revisionName,status.latestReadyRevisionName)' | tr ';\t' '\n' | sort -u); do
      [[ -n "$rev" ]] || continue
      echo "$(gcloud run revisions describe "$rev" --region "$REGION" --project "$PROJECT" \
        --format='value(status.imageDigest)' | sed 's/.*@//') $svc/$rev"
    done
  done
  job=trend-trawler-bandit-traffic
  img="$(gcloud run jobs describe "$job" --region "$REGION" --project "$PROJECT" \
    --format='value(spec.template.spec.template.spec.containers[0].image)')"
  echo "$(resolve_digest "$img") job/$job"
  img="$(gcloud run services describe trend-trawler-api --region "$REGION" --project "$PROJECT" --format=json |
    python3 -I -c 'import json,sys
c = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
print(next((e.get("value", "") for e in c.get("env", []) if e["name"] == "BANDIT_SERVING_IMAGE"), ""))')"
  if [[ -n "$img" ]]; then echo "$(resolve_digest "$img") env/BANDIT_SERVING_IMAGE"; fi
}

preview() {
  local status=0 repo file pkg bucket prefix age
  echo
  echo "== Live digests (must never be deletion candidates)"
  live_digests | tee "$WORK/live.txt"
  cut -d' ' -f1 "$WORK/live.txt" >"$WORK/live_digests.txt"

  for repo in "${AR_REPOS[@]}"; do
    file="$(ar_file "$repo")"
    echo
    echo "== Artifact Registry $repo  (policy: ${file#"$HERE"/})"
    echo "-- current cleanup policies:"
    gcloud artifacts repositories describe "$repo" --location "$REGION" --project "$PROJECT" \
      --format='json(cleanupPolicies,cleanupPolicyDryRun)'
    gcloud artifacts packages list --repository "$repo" --location "$REGION" --project "$PROJECT" \
      --format='value(name)' 2>/dev/null | sed 's#.*/##' >"$WORK/packages.txt"
    echo "-- simulated (untagged AND older than the rule AND not among the newest keepCount):"
    for pkg in $("${PY[@]}" ar-packages "$file" "$WORK/packages.txt"); do
      gcloud artifacts docker images list \
        "$REGION-docker.pkg.dev/$PROJECT/$repo/$pkg" --include-tags --format=json \
        >"$WORK/versions.json" 2>/dev/null
      "${PY[@]}" ar-preview "$file" "$pkg" "$WORK/versions.json" "$WORK/live_digests.txt" || status=$?
    done
  done

  for bucket in "${BUCKETS[@]}"; do
    file="$(gcs_file "$bucket")"
    echo
    echo "== Bucket gs://$bucket  (lifecycle: ${file#"$HERE"/})"
    echo "-- current lifecycle:"
    gcloud storage buckets describe "gs://$bucket" --project "$PROJECT" --format='json(lifecycle_config)'
    echo "-- simulated:"
    while read -r age prefix; do
      echo "  ${prefix} (age ${age}d)"
      gcloud storage ls -l -r "gs://$bucket/${prefix}**" --project "$PROJECT" >"$WORK/ls.txt" 2>/dev/null || true
      "${PY[@]}" gcs-preview "$age" "$WORK/ls.txt"
    done < <(python3 -I -c 'import json,sys
for r in json.load(open(sys.argv[1]))["rule"]:
    for p in r["condition"]["matchesPrefix"]:
        print(r["condition"]["age"], p)' "$file")
  done

  echo
  if [[ "$status" -ne 0 ]]; then
    echo "FAIL: a live digest is a deletion candidate (see '!!' above). Do not apply." >&2
    return "$status"
  fi
  echo "OK: no live digest is a deletion candidate. Preview only; nothing was changed."
}

run() {
  echo "+ $*"
  "$@"
}

apply() {
  local repo file bucket verdict
  # Refuse up front, before changing anything, if any target would lose existing config.
  for repo in "${AR_REPOS[@]}"; do
    file="$(ar_file "$repo")"
    gcloud artifacts repositories describe "$repo" --location "$REGION" --project "$PROJECT" \
      --format='json(cleanupPolicies)' >"$WORK/ar-$repo.json"
    verdict="$("${PY[@]}" ar-same "$file" "$WORK/ar-$repo.json")"
    if [[ "$verdict" == differs ]]; then
      echo "REFUSING: $repo already has different cleanup policies (set-cleanup-policies replaces them)." >&2
      echo "current:" >&2
      cat "$WORK/ar-$repo.json" >&2
      echo "wanted: $file — merge them into one file, then re-run." >&2
      exit 4
    fi
  done
  for bucket in "${BUCKETS[@]}"; do
    file="$(gcs_file "$bucket")"
    gcloud storage buckets describe "gs://$bucket" --project "$PROJECT" \
      --format='json(lifecycle_config)' >"$WORK/gcs-$bucket.json"
    verdict="$("${PY[@]}" gcs-same "$file" "$WORK/gcs-$bucket.json")"
    if [[ "$verdict" == differs ]]; then
      echo "REFUSING: gs://$bucket already has a different lifecycle (--lifecycle-file replaces it)." >&2
      echo "current:" >&2
      cat "$WORK/gcs-$bucket.json" >&2
      echo "wanted: $file — merge the rules into one file, then re-run." >&2
      exit 4
    fi
  done

  for repo in "${AR_REPOS[@]}"; do
    run gcloud artifacts repositories set-cleanup-policies "$repo" \
      --project "$PROJECT" --location "$REGION" --policy "$(ar_file "$repo")" --no-dry-run
  done
  for bucket in "${BUCKETS[@]}"; do
    run gcloud storage buckets update "gs://$bucket" --project "$PROJECT" \
      --lifecycle-file="$(gcs_file "$bucket")"
  done
}

if [[ "$MODE" == --apply ]]; then
  # Never apply against a state that would delete a live image.
  preview
  apply
else
  preview
fi
