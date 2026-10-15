"""deployment/bandit/deploy_traffic_job.sh with a stub ``gcloud`` on PATH (no GCP).

The job must run the exact build the endpoint serves, so it deploys by digest
(``IMAGE`` env var or the first argument); ``IMAGE_TAG`` is only a fallback that
warns, because a tag can be moved after the build.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "bandit"
    / "deploy_traffic_job.sh"
)
REPO = "us-central1-docker.pkg.dev/proj/cpr/trend-trawler-bandit"
DIGEST = f"{REPO}@sha256:{'a' * 64}"

pytestmark = pytest.mark.subprocess


def _run(tmp_path: Path, *args: str, **env_over: str) -> subprocess.CompletedProcess:
    stub = tmp_path / "gcloud"
    stub.write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path}/calls"\n')
    stub.chmod(0o755)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"IMAGE", "IMAGE_TAG", "GCP_REGION", "BANDIT_TRAFFIC_JOB"}
    }
    env |= {
        "PATH": f"{tmp_path}:{env['PATH']}",
        "GOOGLE_CLOUD_PROJECT": "proj",
        "BQ_DATASET_ID": "trend_trawler",
        **env_over,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True
    )


def _image_flag(tmp_path: Path) -> str:
    (call,) = (tmp_path / "calls").read_text().splitlines()
    words = call.split()
    return words[words.index("--image") + 1]


def test_deploys_the_digest_from_the_image_env_without_warning(tmp_path):
    res = _run(tmp_path, IMAGE=DIGEST)
    assert res.returncode == 0, res.stderr
    assert _image_flag(tmp_path) == DIGEST
    assert "WARNING" not in res.stderr


def test_deploys_the_digest_given_as_the_first_argument(tmp_path):
    res = _run(tmp_path, DIGEST, IMAGE=f"{REPO}@sha256:{'b' * 64}")
    assert res.returncode == 0, res.stderr
    assert _image_flag(tmp_path) == DIGEST  # the argument wins over IMAGE
    assert "WARNING" not in res.stderr


def test_digest_wins_over_image_tag(tmp_path):
    res = _run(tmp_path, IMAGE=DIGEST, IMAGE_TAG="abc1234")
    assert res.returncode == 0, res.stderr
    assert _image_flag(tmp_path) == DIGEST
    assert "WARNING" not in res.stderr


def test_image_tag_is_a_fallback_that_warns(tmp_path):
    res = _run(tmp_path, IMAGE_TAG="abc1234")
    assert res.returncode == 0, res.stderr
    assert _image_flag(tmp_path) == f"{REPO}:abc1234"
    assert "WARNING" in res.stderr and "digest" in res.stderr


def test_a_tagged_image_env_warns(tmp_path):
    res = _run(tmp_path, IMAGE=f"{REPO}:latest")
    assert res.returncode == 0, res.stderr
    assert _image_flag(tmp_path) == f"{REPO}:latest"
    assert "WARNING" in res.stderr


def test_no_image_at_all_fails_before_gcloud(tmp_path):
    res = _run(tmp_path)
    assert res.returncode != 0
    assert "IMAGE" in res.stderr
    assert not (tmp_path / "calls").exists()


def test_static_job_settings_are_unchanged(tmp_path):
    res = _run(tmp_path, IMAGE=DIGEST)
    assert res.returncode == 0, res.stderr
    (call,) = (tmp_path / "calls").read_text().splitlines()
    assert call.startswith("run jobs deploy trend-trawler-bandit-traffic ")
    for flag in (
        "--command python",
        "--args=-m,bandit_traffic.main",
        "--region us-central1",
        "--task-timeout 3600",
        "--max-retries 0",
        "--service-account tt-bandit-traffic-sa@proj.iam.gserviceaccount.com",
    ):
        assert flag in call
    assert "BQ_DATASET_ID=trend_trawler" in call
