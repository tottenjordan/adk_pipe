"""Pure staging helpers of ``deployment/bandit/build_image.py`` (no docker)."""

import json
import subprocess
import sys

from bandit.config import load_experiment_config
from deployment.bandit import build_image as bi


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
        ".dockerignore",
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
        "requirements.txt",
    ]
    reqs = (src / "requirements.txt").read_text()
    assert "jax[cpu]==0.11.2" in reqs and "google-cloud-storage" in reqs


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
