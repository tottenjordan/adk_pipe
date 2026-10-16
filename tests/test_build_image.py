"""Pure staging helpers of ``deployment/bandit/build_image.py`` (no docker)."""

import json
import os
import subprocess
import sys

import pytest

from bandit.config import load_experiment_config
from deployment.bandit import build_image as bi
from tests.repo_layout import BANDIT_DIRS

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
        "bandit_stack/bandit/__init__.py",
        "bandit_stack/bandit/aggregate.py",
        "bandit_stack/bandit/baselines.py",
        "bandit_stack/bandit/calibration.py",
        "bandit_stack/bandit/config.py",
        "bandit_stack/bandit/environment.py",
        "bandit_stack/bandit/features.py",
        "bandit_stack/bandit/linear_ts.py",
        "bandit_stack/bandit/metrics.py",
        "bandit_stack/bandit/policies.py",
        "bandit_stack/bandit/scenario.py",
        "bandit_stack/bandit/scenarios/clear_winner.yaml",
        "bandit_stack/bandit/scenarios/drift.yaml",
        "bandit_stack/bandit/scenarios/segment_winners.yaml",
        "bandit_stack/bandit/shifts.py",
        "bandit_stack/bandit/simulate.py",
        "bandit_stack/bandit_serving/__init__.py",
        "bandit_stack/bandit_serving/checkpoint.py",
        "bandit_stack/bandit_serving/instances.py",
        "bandit_stack/bandit_serving/kernels.py",
        "bandit_stack/bandit_serving/predictor.py",
        "bandit_stack/bandit_serving/requirements.txt",
        "bandit_stack/bandit_serving/storage.py",
        "bandit_stack/bandit_traffic/__init__.py",
        "bandit_stack/bandit_traffic/bq.py",
        "bandit_stack/bandit_traffic/endpoint_client.py",
        "bandit_stack/bandit_traffic/main.py",
        "bandit_stack/bandit_traffic/traffic.py",
    ]
    reqs = (src / "bandit_stack/bandit_serving/requirements.txt").read_text()
    assert "jax[cpu]==0.11.2" in reqs and "google-cloud-storage" in reqs


def test_staged_traffic_files_mirror_the_package():
    """One image serves the endpoint AND runs the traffic job: every
    ``bandit_serving`` and ``bandit_traffic`` module ships, and every ``bandit/``
    module except the CLI."""
    serving = sorted(p.name for p in BANDIT_DIRS["bandit_serving"].glob("*.py"))
    assert list(bi.SERVING_FILES) == serving
    pkg = sorted(p.name for p in BANDIT_DIRS["bandit_traffic"].glob("*.py"))
    assert list(bi.TRAFFIC_FILES) == pkg
    core = sorted(p.name for p in BANDIT_DIRS["bandit"].glob("*.py"))
    assert sorted(bi.BANDIT_RUNTIME_FILES) == [n for n in core if n != "cli.py"]


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


SERVING_DOCKERFILE = BANDIT_DIRS["bandit_serving"] / "Dockerfile"


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
    assert any("bandit_stack/bandit_serving/" in ins[i][1] for i in code_copies)
    # the builder sees only requirements.txt, so a code change never re-installs
    before = [a for k, a in ins[: installs[0]] if k == "COPY" and "--from" not in a]
    assert before == [
        "bandit_stack/bandit_serving/requirements.txt /build/requirements.txt"
    ]


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
    reqs = (BANDIT_DIRS["bandit_serving"] / "requirements.txt").read_text()
    lines = {ln.strip() for ln in reqs.splitlines()}
    assert "google-cloud-aiplatform[prediction]==2.4.0" in lines
    assert "protobuf==6.33.6" in lines
    # the traffic job's runtime deps (BigQuery writer, local-URL client)
    assert "google-cloud-bigquery==3.46.1" in lines
    assert "requests==2.34.2" in lines


def test_single_bandit_dockerfile():
    """The traffic job runs the serving image with a command override; there is
    no second Dockerfile or requirements file to drift."""
    assert not (BANDIT_DIRS["bandit_traffic"] / "Dockerfile").exists()
    assert not (BANDIT_DIRS["bandit_traffic"] / "requirements.txt").exists()
    final = _final_stage(_instructions(SERVING_DOCKERFILE))
    copies = [a for k, a in final if k == "COPY" and "--from" not in a]
    assert "bandit_stack/bandit_traffic/*.py /usr/app/bandit_traffic/" in copies


def test_dockerignore_allowlists_exactly_the_staged_sources(tmp_path):
    """The per-Dockerfile ignore file is the only filter on the repo-root (Cloud
    Build) context; it must let through exactly what stage_src_dir copies."""
    src = bi.stage_src_dir(tmp_path / "src")
    ignore = (BANDIT_DIRS["bandit_serving"] / "Dockerfile.dockerignore").read_text()
    allowed = sorted(
        ln[1:] for ln in ignore.splitlines() if ln.startswith("!") and "*" not in ln
    )
    globbed = [ln[1:] for ln in ignore.splitlines() if ln.startswith("!") and "*" in ln]
    assert globbed == ["bandit_stack/bandit/scenarios/*.yaml"]
    expected = [
        f
        for f in bi.staged_files(src)
        if not f.startswith(("Dockerfile", "bandit_stack/bandit/scenarios/"))
    ]
    assert allowed == expected
    head = ignore.split("\n!", 1)[0].splitlines()
    assert "**" in head  # deny everything before the allowlist


def test_staged_dockerfile_copy_sources_exist_in_the_staged_tree(tmp_path):
    """The staged context mirrors the repo (``bandit_stack/...``), so every COPY
    source of the staged Dockerfile must match files in it, or ``docker build``
    fails (and the repo-root Cloud Build context has the same layout)."""
    src = bi.stage_src_dir(tmp_path / "src")
    copies = [
        a.split()
        for k, a in _instructions(src / "Dockerfile")
        if k == "COPY" and "--from" not in a
    ]
    sources = [s for args in copies for s in args[:-1]]
    assert sources, copies
    missing = [s for s in sources if not any(src.glob(s.rstrip("/")))]
    assert missing == [], missing
    assert all(s.startswith(f"{bi.STACK_DIR}/") for s in sources), sources


def test_root_requirements_stay_jax_free():
    assert "jax" not in (bi.REPO_ROOT / "requirements.txt").read_text().lower()


def test_staged_tree_is_self_contained(tmp_path):
    """The staged copy imports + serves with only its own files on sys.path
    (catches a runtime module missing from BANDIT_RUNTIME_FILES)."""
    src = bi.stage_src_dir(tmp_path / "src")
    art = tmp_path / "art"
    art.mkdir()
    (art / "experiment.json").write_text(json.dumps(bi.sample_experiment()))
    hidden = ("", str(bi.REPO_ROOT), str(bi.REPO_ROOT / bi.STACK_DIR))
    src_s, art_s = str(bi.staged_package_root(src)), str(art)
    code = (
        f"import sys, json; sys.path = [p for p in sys.path if p not in {hidden!r}];"
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


def test_staged_tree_runs_the_traffic_job(tmp_path):
    """The staged copy runs ``python -m bandit_traffic.main`` (the job's command
    override) from its package root (the image's WORKDIR), with only its own files."""
    src = bi.stage_src_dir(tmp_path / "src")
    cfg = tmp_path / "experiment.json"
    cfg.write_text(json.dumps({**bi.sample_experiment(), "horizon": 200}))
    out = tmp_path / "out"
    hidden = ("", str(bi.REPO_ROOT), str(bi.REPO_ROOT / bi.STACK_DIR))
    pkg_root = bi.staged_package_root(src)
    code = (
        f"import sys, runpy; sys.path = [p for p in sys.path if p not in {hidden!r}];"
        "sys.path.insert(0, sys.argv[1]);"
        "import bandit, bandit_traffic;"
        "assert bandit_traffic.__file__.startswith(sys.argv[1]), bandit_traffic.__file__;"
        "assert bandit.__file__.startswith(sys.argv[1]), bandit.__file__;"
        "sys.argv = ['bandit_traffic.main', *sys.argv[2:]];"
        "runpy.run_module('bandit_traffic.main', run_name='__main__')"
    )
    res = subprocess.run(
        [
            sys.executable, "-c", code, str(pkg_root), "--in-process", "--dry-run",
            "--config", str(cfg), "--out", str(out),
        ],
        cwd=pkg_root,
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr[-2000:]
    assert any(out.iterdir())


def test_load_staged_predictor_resolves_cpr_module_path(tmp_path):
    from google.cloud.aiplatform.utils import prediction_utils

    src = bi.stage_src_dir(tmp_path / "src")
    cls = bi.load_staged_predictor(src)
    pkg_root = str(bi.staged_package_root(src))
    assert prediction_utils.inspect_source_from_class(cls, pkg_root) == (
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


def test_check_raises_explicitly():
    bi._check(True, "unused")
    with pytest.raises(AssertionError, match="boom"):
        bi._check(False, "boom")


def test_local_test_has_no_bare_asserts():
    """`assert` is stripped under `python -O`, which would report a PASSED local
    test with no checks run; the local test must use `_check` instead."""
    import ast
    import inspect

    for fn in (bi.run_local_test, bi._predict):
        tree = ast.parse(inspect.getsource(fn))
        asserts = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]
        assert not asserts, (fn.__name__, asserts)
