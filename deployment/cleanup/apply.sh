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
# deploy for bandit experiments) is a deletion candidate. It fails (exit 5) when that live
# set can't be trusted: a describe/lookup failed, a service yielded no revision, a digest
# is not `sha256:<64 hex>`, the job image didn't resolve, or a listing failed (a bucket
# prefix that matches no objects is fine). --apply stops on both.
#
# --apply refuses when a repo already has a cleanup policy that is not ours (name not
# `tt-`-prefixed: another team's rule in the shared repo), or a bucket a lifecycle, that
# differs from the file here: set-cleanup-policies and --lifecycle-file both REPLACE the
# whole configuration, so merge by hand first. A repo whose current policies are all ours
# but differ is updated (the diff is printed). Docs: deployment/README.md -> Retention.
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
# Every gcloud call is captured into a variable first: under `set -e` a failing command
# substitution inside a `for` list or an `echo` argument does NOT abort, which would
# silently drop a live digest from the protected set.
UNTRUSTED_EXIT=5
SERVICES=(trend-trawler-api trend-trawler-web trend-trawler-share creative-trawler-crf creative-worker-crf)
JOB=trend-trawler-bandit-traffic
DIGEST_RE='^sha256:[0-9a-f]{64}$'

die() {
  echo "FAIL: $*. Do not apply." >&2
  exit "$UNTRUSTED_EXIT"
}

resolve_digest() { # image ref (tag or digest) -> sha256:...
  local ref="$1" digest
  if [[ "$ref" == *@sha256:* ]]; then
    echo "${ref##*@}"
  else
    digest="$(gcloud artifacts docker images describe "$ref" --format='value(image_summary.digest)')" ||
      die "could not resolve image $ref"
    echo "$digest"
  fi
}

live_digests() { # -> "<digest> <origin>" lines; exits 5 on any failed lookup
  local svc out revs rev digest img api_json
  for svc in "${SERVICES[@]}"; do
    # Every revision receiving traffic (pinned rollouts can split), plus the latest ready.
    out="$(gcloud run services describe "$svc" --region "$REGION" --project "$PROJECT" \
      --format='value(status.traffic[].revisionName,status.latestReadyRevisionName)')" ||
      die "could not describe service $svc"
    revs="$(tr ';\t' '\n' <<<"$out" | sed '/^$/d' | sort -u)"
    [[ -n "$revs" ]] || die "service $svc has no revisions"
    for rev in $revs; do
      digest="$(gcloud run revisions describe "$rev" --region "$REGION" --project "$PROJECT" \
        --format='value(status.imageDigest)')" || die "could not describe revision $svc/$rev"
      echo "${digest##*@} $svc/$rev"
    done
  done
  img="$(gcloud run jobs describe "$JOB" --region "$REGION" --project "$PROJECT" \
    --format='value(spec.template.spec.template.spec.containers[0].image)')" ||
    die "could not describe job $JOB"
  [[ -n "$img" ]] || die "job $JOB has no image"
  digest="$(resolve_digest "$img")"
  echo "$digest job/$JOB"
  api_json="$(gcloud run services describe trend-trawler-api --region "$REGION" --project "$PROJECT" --format=json)" ||
    die "could not describe service trend-trawler-api (env)"
  img="$(python3 -I -c 'import json,sys
c = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
print(next((e.get("value", "") for e in c.get("env", []) if e["name"] == "BANDIT_SERVING_IMAGE"), ""))' <<<"$api_json")" ||
    die "could not read BANDIT_SERVING_IMAGE from trend-trawler-api"
  if [[ -n "$img" ]]; then
    digest="$(resolve_digest "$img")"
    echo "$digest env/BANDIT_SERVING_IMAGE"
  fi
}

validate_live() { # live.txt -> exits 5 unless every digest is well formed and every origin is present
  local file="$1" bad=0 digest origin svc
  while read -r digest origin; do
    if ! [[ "$digest" =~ $DIGEST_RE ]]; then
      echo "FAIL: malformed digest '${digest}' for ${origin:-?}" >&2
      bad=1
    fi
  done <"$file"
  for svc in "${SERVICES[@]}"; do
    grep -q " $svc/" "$file" || {
      echo "FAIL: no live revision digest collected for service $svc" >&2
      bad=1
    }
  done
  grep -q " job/$JOB\$" "$file" || {
    echo "FAIL: job $JOB image digest not resolved" >&2
    bad=1
  }
  [[ "$bad" -eq 0 ]] || die "the live digest set is incomplete"
}

gcs_ls() { # bucket url pattern -> ls -l listing on stdout; a pattern matching no objects is empty
  local err
  err="$WORK/ls.err"
  if gcloud storage ls -l -r "$1" --project "$PROJECT" 2>"$err"; then return 0; fi
  if grep -q "matched no objects" "$err"; then return 0; fi
  cat "$err" >&2
  die "could not list $1"
}

preview() {
  local status=0 rc repo file pkg pkgs bucket prefix age
  echo
  echo "== Live digests (must never be deletion candidates)"
  live_digests >"$WORK/live.txt"
  cat "$WORK/live.txt"
  validate_live "$WORK/live.txt"
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
    pkgs="$("${PY[@]}" ar-packages "$file" "$WORK/packages.txt")"
    for pkg in $pkgs; do
      gcloud artifacts docker images list \
        "$REGION-docker.pkg.dev/$PROJECT/$repo/$pkg" --include-tags --format=json \
        >"$WORK/versions.json" 2>/dev/null
      rc=0
      "${PY[@]}" ar-preview "$file" "$pkg" "$WORK/versions.json" "$WORK/live_digests.txt" || rc=$?
      case "$rc" in
        0) ;;
        3) status=3 ;;
        *) die "simulating $repo/$pkg failed (exit $rc)" ;;
      esac
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
      gcs_ls "gs://$bucket/${prefix}**" >"$WORK/ls.txt"
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
    if [[ "$verdict" == ours ]]; then
      echo "$repo: current cleanup policies are all ours (tt-*) but differ from $file; replacing them:"
      "${PY[@]}" ar-diff "$file" "$WORK/ar-$repo.json"
    elif [[ "$verdict" == differs ]]; then
      echo "REFUSING: $repo has cleanup policies that are not ours (not tt-*) and differ (set-cleanup-policies replaces them)." >&2
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
