"""Build (and optionally local-test / push) the CPR bandit serving image.

    uv run python deployment/bandit/build_image.py --local-test   # build + local endpoint
    uv run python deployment/bandit/build_image.py --push         # build + docker push

Stages a temporary ``src_dir`` holding ``bandit_serving/`` (the predictor), the
runtime subset of ``bandit/`` (config, features, linear_ts, aggregate +
``scenarios/*.yaml``) and ``bandit_serving/requirements.txt``, then calls
``LocalModel.build_cpr_model`` (the CPR SDK writes the Dockerfile: base image +
requirements + ``google-cloud-aiplatform[prediction]`` + the CPR model server).

The image is ``{region}-docker.pkg.dev/{project}/{repository}/trend-trawler-bandit:{git_sha}``.
``--local-test`` serves it with ``deploy_to_local_endpoint`` (docker, no GCP)
against a sample ``experiment.json`` and ``VERTEX_CPR_WEB_CONCURRENCY=1``, then
checks reset / decision / reward / state round-trips and prints timings.
Nothing here touches GCP except ``--push`` (``docker push``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_NAME = "trend-trawler-bandit"
DEFAULT_BASE_IMAGE = "python:3.13-slim"
SERVING_ENV = {"VERTEX_CPR_WEB_CONCURRENCY": "1"}  # == endpoint.SERVING_ENV
SERVING_FILES = ("__init__.py", "predictor.py")
#: ``bandit/`` modules the predictor needs at runtime (no simulator/baselines).
BANDIT_RUNTIME_FILES = (
    "__init__.py",
    "aggregate.py",
    "config.py",
    "features.py",
    "linear_ts.py",
)
PREDICTOR_MODULE = "bandit_serving.predictor"
FIXTURE_DETAIL = (
    REPO_ROOT / "frontend/scripts/screenshot-fixtures/experiment-detail.json"
)
SAMPLE_CONTEXT = {
    "devicetype": "mobile",
    "os": "ios",
    "connectiontype": "wifi",
    "region": "south",
    "age_bucket": "21-34",
    "daypart": "evening",
    "weekend": False,
    "topic_matches_trend": True,
    "interest_matches_product": False,
    "freq_24h": "0",
}


# ----------------------------------------------------------------- pure helpers


def image_uri(
    project: str, tag: str, region: str = "us-central1", repository: str = "cpr"
) -> str:
    return f"{region}-docker.pkg.dev/{project}/{repository}/{IMAGE_NAME}:{tag}"


def git_sha(repo: Path = REPO_ROOT) -> str:
    """Short HEAD sha, suffixed ``-dirty`` when tracked files have local edits."""
    sha = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--"], cwd=repo, check=False
    ).returncode
    return f"{sha}-dirty" if dirty else sha


def stage_src_dir(dest: Path, repo: Path = REPO_ROOT) -> Path:
    """Copy the serving sources into ``dest`` (the CPR ``src_dir``)."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "bandit_serving").mkdir(exist_ok=True)
    for name in SERVING_FILES:
        shutil.copy2(repo / "bandit_serving" / name, dest / "bandit_serving" / name)
    (dest / "bandit" / "scenarios").mkdir(parents=True, exist_ok=True)
    for name in BANDIT_RUNTIME_FILES:
        shutil.copy2(repo / "bandit" / name, dest / "bandit" / name)
    for yaml_file in sorted((repo / "bandit" / "scenarios").glob("*.yaml")):
        shutil.copy2(yaml_file, dest / "bandit" / "scenarios" / yaml_file.name)
    shutil.copy2(
        repo / "bandit_serving" / "requirements.txt", dest / "requirements.txt"
    )
    (dest / ".dockerignore").write_text("**/__pycache__\n**/*.pyc\n")
    return dest


def staged_files(src_dir: Path) -> list[str]:
    return sorted(
        p.relative_to(src_dir).as_posix() for p in src_dir.rglob("*") if p.is_file()
    )


def load_staged_predictor(src_dir: Path) -> Any:
    """Import ``BanditPredictor`` from the *staged* file: ``build_cpr_model``
    requires the class's source to live under ``src_dir`` and derives
    ``PREDICTOR_MODULE`` (``bandit_serving.predictor``) from that path."""
    path = src_dir / "bandit_serving" / "predictor.py"
    name = f"_staged_bandit_predictor_{abs(hash(str(src_dir)))}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.BanditPredictor


def sample_arms() -> list[dict[str, Any]]:
    """3 ``ArmSpec`` dicts from the screenshot fixture (synthetic fallback)."""
    try:
        arms = json.loads(FIXTURE_DETAIL.read_text())["arms"][:3]
        return [
            {
                "creative_id": a["creativeId"],
                "label": a["label"],
                "scores": {k: float(v) for k, v in a["scores"].items()},
                "visual_style": "",
            }
            for a in arms
        ]
    except (OSError, KeyError, ValueError):
        # no `bandit` import here: deployment/ is imported by the api, which must
        # stay jax/bandit-free (tests/test_bandit_simulate_metrics.py guards it)
        return [
            {"creative_id": f"synthetic-{c}", "label": f"Synthetic {c}",
             "scores": {"overall": o}}
            for c, o in (("a", 0.82), ("b", 0.74), ("c", 0.66))
        ]  # fmt: skip


def sample_experiment(experiment_id: str = "localtest") -> dict[str, Any]:
    """A §1 ``experiment.json`` without ``policy.noise_var`` (so the container
    exercises the calibrated default)."""
    return {
        "experiment_id": experiment_id,
        "arms": sample_arms(),
        "scenario": "clear_winner",
        "ctr_mode": "demo",
        "reward_mode": "click",
        "horizon": 20000,
        "batch_size": 100,
        "episodes": 1,
        "seed": 1,
        "policy": {"min_propensity": 0.02},
    }


# ------------------------------------------------------------------ docker ops


def build(src_dir: Path, uri: str, base_image: str, no_cache: bool = False) -> Any:
    from google.cloud.aiplatform.prediction import LocalModel

    predictor_cls = load_staged_predictor(src_dir)
    return LocalModel.build_cpr_model(
        str(src_dir),
        uri,
        predictor=predictor_cls,
        base_image=base_image,
        requirements_path=str(src_dir / "requirements.txt"),
        no_cache=no_cache,
    )


def image_size_mb(uri: str) -> float:
    out = subprocess.run(
        ["docker", "image", "inspect", uri, "--format", "{{.Size}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return int(out) / 1e6


def _predict(endpoint: Any, instances: list, parameters: dict | None = None) -> list:
    body: dict[str, Any] = {"instances": instances}
    if parameters:
        body["parameters"] = parameters
    resp = endpoint.predict(
        request=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    if resp.status_code != 200:
        raise AssertionError(f"predict HTTP {resp.status_code}: {resp.text[:500]}")
    preds = resp.json()["predictions"]
    assert len(preds) == len(instances), (len(preds), len(instances))
    return preds


def _timed(fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, float]:
    t0 = time.perf_counter()
    out = fn(*args, **kwargs)
    return out, (time.perf_counter() - t0) * 1000.0


def _decisions(n: int, prefix: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "decision",
            "request_id": f"{prefix}{i}",
            "ts": i,
            "context": SAMPLE_CONTEXT,
        }
        for i in range(n)
    ]


def run_local_test(local_model: Any, ready_timeout: int = 600) -> dict[str, float]:
    """Serve the image locally and exercise every instance type."""
    from google.cloud import aiplatform

    for name, value in SERVING_ENV.items():  # the image has no ENV for this
        local_model.serving_container_spec.env.append(
            aiplatform.gapic.EnvVar(name=name, value=value)
        )
    cfg = sample_experiment()
    arm_ids = {a["creative_id"] for a in cfg["arms"]}
    timings: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="bandit-artifacts-") as art:
        Path(art, "experiment.json").write_text(json.dumps(cfg))
        endpoint = local_model.deploy_to_local_endpoint(
            artifact_uri=art, container_ready_timeout=ready_timeout
        )
        t0 = time.perf_counter()
        endpoint.serve()
        timings["cold_start_s"] = time.perf_counter() - t0
        try:
            logs = endpoint.container.logs().decode("utf-8", "replace")
            assert "model server workers to 1" in logs, "expected a single CPR worker"

            reset, timings["reset_ms"] = _timed(
                _predict, endpoint, [{"type": "reset", "episode": 1, "seed": 7}]
            )
            assert reset[0] == {
                "type": "reset",
                "episode": 1,
                "model_version": f"{cfg['experiment_id']}-e1-v0",
            }, reset

            first, timings["decision20_first_ms"] = _timed(
                _predict, endpoint, _decisions(20, "a")
            )
            for d in first:
                assert d["type"] == "decision" and d["chosen_arm"] in arm_ids, d
                assert abs(sum(d["arm_probabilities"].values()) - 1.0) < 1e-4, d
                assert min(d["arm_probabilities"].values()) >= 0.02 - 1e-6, d
            v0 = first[0]["model_version"]

            rewards = [
                {
                    "type": "reward",
                    "request_id": d["request_id"],
                    "arm": d["chosen_arm"],
                    "reward": float(i % 2),
                    "clicked": i % 2,
                }
                for i, d in enumerate(first)
            ]
            acks, timings["reward20_ms"] = _timed(_predict, endpoint, rewards)
            assert all(a["accepted"] for a in acks), acks
            dup, _ = _timed(_predict, endpoint, rewards[:1])
            assert dup[0]["accepted"] is False, dup

            second, timings["decision20_new_params_ms"] = _timed(
                _predict, endpoint, _decisions(20, "b"), {"exploration_scale": 1.5}
            )
            assert second[0]["model_version"] != v0, (v0, second[0])
            assert second[0]["model_version"] == f"{cfg['experiment_id']}-e1-v1"

            warm = []
            for k in range(5):
                _, ms = _timed(_predict, endpoint, _decisions(20, f"w{k}-"))
                warm.append(ms)
            timings["decision20_warm_median_ms"] = sorted(warm)[len(warm) // 2]
            warm = []
            for k in range(5):
                _, ms = _timed(_predict, endpoint, _decisions(100, f"h{k}-"))
                warm.append(ms)
            timings["decision100_warm_median_ms"] = sorted(warm)[len(warm) // 2]

            state, timings["state_ms"] = _timed(_predict, endpoint, [{"type": "state"}])
            s = state[0]
            assert s["type"] == "state" and s["step"] == 20, s
            assert sum(s["pulls"].values()) == 20 and set(s["pulls"]) == arm_ids, s
            assert s["feature_spec_version"] == "ctx-v1", s

            mixed = _predict(endpoint, [{"type": "nope"}, {"type": "state"}])
            assert [m["type"] for m in mixed] == ["error", "state"], mixed

            logs = endpoint.container.logs().decode("utf-8", "replace")
            assert "checkpoint write failed" not in logs, logs[-2000:]
        except BaseException:
            endpoint.print_container_logs(show_all=True)
            raise
        finally:
            endpoint.stop()
    return timings


def push(uri: str) -> None:
    subprocess.run(["docker", "push", uri], check=True)


# ------------------------------------------------------------------------- CLI


def _define_flags() -> None:  # pragma: no cover - manual CLI
    from absl import flags

    flags.DEFINE_string(
        "project",
        os.getenv("GOOGLE_CLOUD_PROJECT", "local"),
        "GCP project (image path)",
    )
    flags.DEFINE_string("region", "us-central1", "Artifact Registry region")
    flags.DEFINE_string("repository", "cpr", "Artifact Registry repository")
    flags.DEFINE_string("tag", None, "image tag (default: git sha)")
    flags.DEFINE_string("base-image", DEFAULT_BASE_IMAGE, "CPR base image")
    flags.DEFINE_bool("no-cache", False, "docker build --no-cache")
    flags.DEFINE_bool("local-test", False, "serve locally and round-trip requests")
    flags.DEFINE_integer("ready-timeout", 600, "local container ready timeout (s)")
    flags.DEFINE_bool("push", False, "docker push the built image")


def _flag(name: str) -> Any:  # pragma: no cover - manual CLI
    from absl import flags

    return flags.FLAGS[name].value


def main(argv: list[str]) -> None:  # pragma: no cover - manual CLI
    del argv
    base_image = _flag("base-image")
    uri = image_uri(
        _flag("project"), _flag("tag") or git_sha(), _flag("region"),
        _flag("repository"),
    )  # fmt: skip
    with tempfile.TemporaryDirectory(prefix="bandit-cpr-src-") as tmp:
        src = stage_src_dir(Path(tmp))
        print(f"staged {len(staged_files(src))} files; building {uri}")
        t0 = time.perf_counter()
        local_model = build(src, uri, base_image, _flag("no-cache"))
        print(f"build: {time.perf_counter() - t0:.1f}s")
    print(f"image: {uri} ({image_size_mb(uri):.0f} MB, base {base_image})")
    if _flag("local-test"):
        timings = run_local_test(local_model, _flag("ready-timeout"))
        print("local test PASSED")
        for k, v in timings.items():
            print(f"  {k}: {v:.2f}")
    if _flag("push"):
        push(uri)
        print(f"pushed {uri}")


if __name__ == "__main__":  # pragma: no cover - manual CLI
    from absl import app

    sys.path.insert(0, str(REPO_ROOT))  # run as a script: make `bandit` importable
    _define_flags()
    app.run(main)
