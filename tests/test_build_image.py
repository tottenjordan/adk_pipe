"""Pure staging helpers of ``deployment/bandit/build_image.py`` (no docker)."""

import json
import os
import subprocess
import sys

import pytest

from bandit.config import load_experiment_config
from deployment.bandit import build_image as bi

pytestmark = [pytest.mark.slow, pytest.mark.subprocess]


def test_image_uri():
    assert bi.image_uri("proj", "abc123") == (
        "us-central1-docker.pkg.dev/proj/cpr/trend-trawler-bandit:abc123"
    )
    assert bi.image_uri("p", "t", "europe-west4", "repo").startswith(
        "europe-west4-docker.pkg.dev/p/repo/"
    )


def test_staged_tree_contents(tmp_path):
    src = bi.stage_src_dir(tmp_path / "src")
    assert bi.staged_files(src) == [
        "Dockerfile",
        "Dockerfile.dockerignore",
        "bandit/__init__.py",
        "bandit/aggregate.py",
        "bandit/config.py",
        "bandit/features.py",
        "bandit/linear_ts.py",
        "bandit/scenarios/clear_winner.yaml",
        "bandit/scenarios/drift.yaml",
        "bandit/scenarios/segment_winners.yaml",
        "bandit_serving/__init__.py",
        "bandit_serving/predictor.py",
        "bandit_serving/requirements.txt",
    ]
    reqs = (src / "bandit_serving/requirements.txt").read_text()
    assert "jax[cpu]==0.11.2" in reqs and "google-cloud-storage" in reqs


def _instructions(path):
    """``(KEYWORD, args)`` per Dockerfile instruction (continuations joined)."""
    out, buf = [], ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        keyword, _, args = buf.partition(" ")
        out.append((keyword.upper(), args.strip()))
        buf = ""
    return out


SERVING_DOCKERFILE = bi.REPO_ROOT / "bandit_serving" / "Dockerfile"


def _final_stage(instructions):
    last_from = max(i for i, (k, _) in enumerate(instructions) if k == "FROM")
    return instructions[last_from:]


def test_serving_dockerfile_installs_deps_once_before_code():
    ins = _instructions(SERVING_DOCKERFILE)
    installs = [i for i, (k, a) in enumerate(ins) if k == "RUN" and "pip install" in a]
    assert len(installs) == 1, installs
    code_copies = [
        i
        for i, (k, a) in enumerate(ins)
        if k == "COPY" and "--from" not in a and "requirements.txt" not in a
    ]
    assert code_copies and all(i > installs[0] for i in code_copies)
    assert any("bandit_serving/" in ins[i][1] for i in code_copies)
    # the builder sees only requirements.txt, so a code change never re-installs
    before = [a for k, a in ins[: installs[0]] if k == "COPY" and "--from" not in a]
    assert before == ["bandit_serving/requirements.txt /build/requirements.txt"]


def test_serving_dockerfile_matches_cpr_sdk_contract():
    from google.cloud.aiplatform.prediction import local_model

    final = _final_stage(_instructions(SERVING_DOCKERFILE))
    env = " ".join(a for k, a in final if k == "ENV")
    for key, value in {
        "PREDICTOR_MODULE": bi.PREDICTOR_MODULE,
        "PREDICTOR_CLASS": "BanditPredictor",
        "HANDLER_MODULE": local_model._DEFAULT_HANDLER_MODULE,
        "HANDLER_CLASS": local_model._DEFAULT_HANDLER_CLASS,
    }.items():
        assert f"{key}={value}" in env, key
    assert ("ENTRYPOINT", json.dumps(
        ["python", "-m", local_model._DEFAULT_PYTHON_MODULE]
    )) in final  # fmt: skip
    assert ("EXPOSE", str(local_model.DEFAULT_HTTP_PORT)) in final
    assert ("WORKDIR", "/usr/app") in final
    users = [a for k, a in final if k == "USER"]
    assert users and users[-1] not in ("root", "0"), users


def test_serving_requirements_pin_cpr_server():
    reqs = (bi.REPO_ROOT / "bandit_serving" / "requirements.txt").read_text()
    lines = {ln.strip() for ln in reqs.splitlines()}
    assert "google-cloud-aiplatform[prediction]==2.4.0" in lines
    assert "protobuf==6.33.6" in lines


def test_dockerignore_allowlists_exactly_the_staged_sources(tmp_path):
    """The per-Dockerfile ignore file is the only filter on the repo-root (Cloud
    Build) context; it must let through exactly what stage_src_dir copies."""
    src = bi.stage_src_dir(tmp_path / "src")
    ignore = (bi.REPO_ROOT / "bandit_serving" / "Dockerfile.dockerignore").read_text()
    allowed = sorted(
        ln[1:] for ln in ignore.splitlines() if ln.startswith("!") and "*" not in ln
    )
    globbed = [ln[1:] for ln in ignore.splitlines() if ln.startswith("!") and "*" in ln]
    assert globbed == ["bandit/scenarios/*.yaml"]
    expected = [
        f
        for f in bi.staged_files(src)
        if not f.startswith(("Dockerfile", "bandit/scenarios/"))
    ]
    assert allowed == expected
    head = ignore.split("\n!", 1)[0].splitlines()
    assert "**" in head  # deny everything before the allowlist


def test_root_requirements_stay_jax_free():
    assert "jax" not in (bi.REPO_ROOT / "requirements.txt").read_text().lower()


def test_staged_tree_is_self_contained(tmp_path):
    """The staged copy imports + serves with only its own files on sys.path
    (catches a runtime module missing from BANDIT_RUNTIME_FILES)."""
    src = bi.stage_src_dir(tmp_path / "src")
    art = tmp_path / "art"
    art.mkdir()
    (art / "experiment.json").write_text(json.dumps(bi.sample_experiment()))
    root, src_s, art_s = str(bi.REPO_ROOT), str(src), str(art)
    code = (
        f"import sys, json; sys.path = [p for p in sys.path if p not in ('', {root!r})];"
        f"sys.path.insert(0, {src_s!r});"
        "import bandit, bandit_serving.predictor as m;"
        f"assert bandit.__file__.startswith({src_s!r}), bandit.__file__;"
        f"p = m.BanditPredictor(); p.load({art_s!r});"
        "out = p.postprocess(p.predict(p.preprocess({'instances': [{'type': 'state'}]})));"
        "print(json.dumps(out))"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "BANDIT_WARMUP": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr[-2000:]
    state = json.loads(res.stdout.strip().splitlines()[-1])["predictions"][0]
    assert state["model_version"] == "localtest-e0-v0"


def test_load_staged_predictor_resolves_cpr_module_path(tmp_path):
    from google.cloud.aiplatform.utils import prediction_utils

    src = bi.stage_src_dir(tmp_path / "src")
    cls = bi.load_staged_predictor(src)
    assert prediction_utils.inspect_source_from_class(cls, str(src)) == (
        bi.PREDICTOR_MODULE,
        "BanditPredictor",
    )


def test_sample_experiment_is_valid_and_uncalibrated():
    cfg = bi.sample_experiment()
    assert len(cfg["arms"]) == 3
    assert "noise_var" not in cfg["policy"]
    load_experiment_config(cfg)  # 0.25 default fills in; still valid


def test_serving_env_matches_endpoint_lib():
    from deployment.bandit import endpoint

    assert bi.SERVING_ENV == endpoint.SERVING_ENV == {"VERTEX_CPR_WEB_CONCURRENCY": "1"}


def test_build_command(tmp_path):
    assert bi.build_command(tmp_path, "img:t") == [
        "docker", "build", "-f", str(tmp_path / "Dockerfile"), "-t", "img:t",
        str(tmp_path),
    ]  # fmt: skip
    cmd = bi.build_command(tmp_path, "img:t", "python:3.13", no_cache=True)
    assert cmd[-4:] == [
        "--no-cache",
        "--build-arg",
        "BASE_IMAGE=python:3.13",
        str(tmp_path),
    ]


def test_local_model_for_matches_build_cpr_model_spec():
    from google.cloud.aiplatform.prediction import local_model

    spec = bi.local_model_for("img@sha256:abc").serving_container_spec
    assert spec.image_uri == "img@sha256:abc"
    assert spec.predict_route == local_model.DEFAULT_PREDICT_ROUTE
    assert spec.health_route == local_model.DEFAULT_HEALTH_ROUTE
