"""deployment/cleanup/apply.sh preview with a stub ``gcloud`` on PATH (no GCP).

A failed lookup of what Cloud Run serves must abort the preview (exit 5) instead of
silently dropping a live digest from the protected set and printing "OK".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deployment" / "cleanup" / "apply.sh"
DIGEST = "sha256:" + "ab" * 32
OK_LINE = "OK: no live digest is a deletion candidate"

# Only the commands the preview path calls; behaviour is switched by STUB_* env vars.
GCLOUD_STUB = r"""#!/usr/bin/env bash
case "$1 $2 $3" in
  "run services describe")
    if [[ "$4" == "${STUB_FAIL_SERVICE:-}" ]]; then echo "ERROR: describe failed" >&2; exit 1; fi
    if [[ "$*" == *--format=json* ]]; then
      echo '{"spec":{"template":{"spec":{"containers":[{"env":[]}]}}}}'
    else
      printf '%s-00001-abc\t%s-00001-abc\n' "$4" "$4"
    fi ;;
  "run revisions describe")
    if [[ -n "${STUB_EMPTY_DIGEST:-}" ]]; then echo ""; else echo "us-docker.pkg.dev/p/r/i@@@DIGEST@@"; fi ;;
  "run jobs describe")
    echo "us-docker.pkg.dev/p/r/job@@@DIGEST@@" ;;
  "artifacts repositories describe")
    echo "{}" ;;
  "artifacts packages list")
    echo "projects/p/locations/l/repositories/r/packages/trend-trawler-api" ;;
  "artifacts docker images")
    echo "[]" ;;
  "storage buckets describe")
    echo "{}" ;;
  "storage ls -l")
    case "${STUB_LS:-}" in
      nomatch) echo "ERROR: (gcloud.storage.ls) One or more URLs matched no objects." >&2; exit 1 ;;
      fail) echo "ERROR: (gcloud.storage.ls) HTTPError 403: permission denied" >&2; exit 1 ;;
    esac ;;
  *) echo "unexpected gcloud call: $*" >&2; exit 99 ;;
esac
""".replace("@@DIGEST@@", DIGEST)


def _run(tmp_path: Path, **stub_env: str) -> subprocess.CompletedProcess:
    stub = tmp_path / "gcloud"
    stub.write_text(GCLOUD_STUB)
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GOOGLE_CLOUD_PROJECT": "test-project",
        **stub_env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120
    )


@pytest.mark.parametrize("ls_mode", ["", "nomatch"])
def test_healthy_preview_reaches_summary(tmp_path, ls_mode):
    proc = _run(tmp_path, STUB_LS=ls_mode)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert OK_LINE in proc.stdout
    assert f"{DIGEST} creative-worker-crf/creative-worker-crf-00001-abc" in proc.stdout


@pytest.mark.parametrize("svc", ["trend-trawler-api", "creative-worker-crf"])
def test_failed_service_describe_aborts(tmp_path, svc):
    proc = _run(tmp_path, STUB_FAIL_SERVICE=svc)
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert OK_LINE not in proc.stdout
    assert svc in proc.stderr


def test_empty_revision_digest_aborts(tmp_path):
    proc = _run(tmp_path, STUB_EMPTY_DIGEST="1")
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert OK_LINE not in proc.stdout


def test_failed_bucket_listing_aborts(tmp_path):
    proc = _run(tmp_path, STUB_LS="fail")
    assert proc.returncode != 0
    assert OK_LINE not in proc.stdout
    assert "permission denied" in proc.stderr
