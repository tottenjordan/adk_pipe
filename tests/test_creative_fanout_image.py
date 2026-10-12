"""CRF container image: one Dockerfile serves both Cloud Run functions.

``creative-trawler-crf`` (orchestrator) and ``creative-worker-crf`` (worker) run the
same image; ``FUNCTION_TARGET`` (set per service) picks the entry point. Named so
the ``tests/test_crf_*.py`` glob of the CRF-deps CI venv (no pyyaml) skips it.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CRF_DIR = REPO_ROOT / "cloud_functions" / "creative_fanout"
DOCKERFILE = CRF_DIR / "Dockerfile"
DOCKERIGNORE = CRF_DIR / "Dockerfile.dockerignore"


def _instructions(path: Path) -> list[tuple[str, str]]:
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


def test_dependencies_install_once_before_code():
    ins = _instructions(DOCKERFILE)
    installs = [i for i, (k, a) in enumerate(ins) if k == "RUN" and "pip install" in a]
    assert len(installs) == 1, installs
    assert "-r /build/requirements.txt" in ins[installs[0]][1]
    copies = [(i, a) for i, (k, a) in enumerate(ins) if k == "COPY"]
    before = [a for i, a in copies if i < installs[0] and "--from" not in a]
    assert before == [
        "cloud_functions/creative_fanout/requirements.txt /build/requirements.txt"
    ]
    code = [i for i, a in copies if "--from" not in a and "requirements.txt" not in a]
    assert code and min(code) > installs[0]


def test_multi_stage_uv_venv_non_root():
    ins = _instructions(DOCKERFILE)
    froms = [a for k, a in ins if k == "FROM"]
    assert len(froms) == 3, froms  # uv, builder, final
    assert ("COPY", "--from=builder /opt/venv /opt/venv") in ins
    last_from = max(i for i, (k, _) in enumerate(ins) if k == "FROM")
    users = [a for k, a in ins[last_from:] if k == "USER"]
    assert users and users[-1] not in ("root", "0"), users


def test_no_function_target_default():
    """Each service sets FUNCTION_TARGET; a baked-in default would let a
    misconfigured service silently run the other entry point."""
    for keyword, args in _instructions(DOCKERFILE):
        if keyword in ("ENV", "ARG"):
            assert "FUNCTION_TARGET" not in args, (keyword, args)


def test_start_command_runs_functions_framework_from_env_target():
    ins = _instructions(DOCKERFILE)
    starts = [(k, a) for k, a in ins if k in ("CMD", "ENTRYPOINT")]
    assert len(starts) == 1, starts
    cmd = starts[0][1]
    # Shell form (via sh -c) so $FUNCTION_TARGET / $PORT expand at start.
    assert cmd.startswith('["/bin/sh", "-c",'), cmd
    assert "exec functions-framework" in cmd
    # Package-style load: main.py's relative imports need --source=main.py.
    assert "--source=main.py" in cmd
    # ${VAR:?msg} makes sh exit at once when the target is unset.
    assert re.search(r"--target=(\\\")?\$\{FUNCTION_TARGET:\?", cmd), cmd
    assert "--signature-type=cloudevent" in cmd
    assert "${PORT:-8080}" in cmd


def test_dockerignore_allowlists_exactly_the_function_files():
    lines = [
        ln.strip()
        for ln in DOCKERIGNORE.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert lines[0] == "**"
    allowed = {ln[1:] for ln in lines[1:]}
    assert all(ln.startswith("!") for ln in lines[1:]), lines
    assert allowed == {
        "cloud_functions/creative_fanout/__init__.py",
        "cloud_functions/creative_fanout/main.py",
        "cloud_functions/creative_fanout/config.py",
        "cloud_functions/creative_fanout/session.py",
        "cloud_functions/creative_fanout/requirements.txt",
    }
    shipped = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in CRF_DIR.iterdir()
        if p.suffix == ".py" or p.name == "requirements.txt"
    }
    assert allowed == shipped


# --- root cloudbuild.yaml: build-crf + optional deploy -----------------------------


def _root_build() -> dict:
    import yaml

    return yaml.safe_load((REPO_ROOT / "cloudbuild.yaml").read_text())


def _step(build: dict, step_id: str) -> dict:
    return next(s for s in build["steps"] if s["id"] == step_id)


def test_build_crf_runs_in_parallel_with_registry_cache_and_prints_digest():
    build = _root_build()
    assert build["substitutions"]["_CRF_IMAGE"] == "${_REPO}/creative-fanout-crf"
    step = _step(build, "build-crf")
    assert step["waitFor"] == ["buildx-setup"]
    script = step["args"][-1]
    assert "--file cloud_functions/creative_fanout/Dockerfile" in script
    assert "--tag ${_CRF_IMAGE}:${SHORT_SHA} --tag ${_CRF_IMAGE}:latest" in script
    assert "--cache-from type=registry,ref=${_CRF_IMAGE}:buildcache" in script
    assert "--cache-to type=registry,ref=${_CRF_IMAGE}:buildcache,mode=max" in script
    assert "--metadata-file /workspace/crf-meta.json ." in script
    assert "test -s /workspace/crf.digest" in script
    assert 'echo "CRF_IMAGE=${_CRF_IMAGE}@$$(cat /workspace/crf.digest)"' in script


def test_optional_deploy_rolls_both_crf_services_without_traffic():
    build = _root_build()
    step = _step(build, "deploy")
    assert "build-crf" in step["waitFor"]
    script = step["args"][-1]
    skip, _, deploy = script.partition("exit 0")
    assert "CRF_IMAGE=" in skip  # digests printed on the default (no-deploy) path
    for service, target in (
        ("creative-trawler-crf", "crf_entrypoint"),
        ("creative-worker-crf", "agent_worker_entrypoint"),
    ):
        cmd = deploy.split(f"gcloud run deploy {service} ", 1)[1].split("gcloud", 1)[0]
        assert '--image "${_CRF_IMAGE}@$$(cat /workspace/crf.digest)"' in cmd
        assert "--no-traffic" in cmd
        assert f"--update-env-vars FUNCTION_TARGET={target}" in cmd
        assert "--clear-base-image" in cmd
        # Never --source/--function (buildpacks) or --set-env-vars (would wipe env).
        assert not re.search(r"--source|--function|--set-env-vars", cmd), cmd
    assert "update-traffic" not in deploy


def test_upload_filter_keeps_only_the_crf_function_dir():
    lines = (REPO_ROOT / "deployment" / "cloudbuild.gcloudignore").read_text()
    lines = [ln.strip() for ln in lines.splitlines()]
    assert "/cloud_functions/*" in lines
    assert "!/cloud_functions/creative_fanout/" in lines
    # Re-excluded inside, then only the build's files (never a local message.json).
    assert "/cloud_functions/creative_fanout/*" in lines
    assert "!/cloud_functions/creative_fanout/*.py" in lines
    assert "/cloud_functions/" not in lines
    assert "__pycache__/" in lines


ROOT_BUILD_STEPS = ("build-api", "build-web", "build-crf")
REPRODUCIBLE_OUTPUT = "--output type=image,push=true,rewrite-timestamp=true"


def test_root_builds_are_reproducible():
    """Same commit -> same digest: timestamps clamped to SOURCE_DATE_EPOCH."""
    build = _root_build()
    assert build["substitutions"]["_SOURCE_DATE_EPOCH"] == "0"
    for step_id in ROOT_BUILD_STEPS:
        script = _step(build, step_id)["args"][-1]
        assert "docker buildx build" in script, step_id
        assert REPRODUCIBLE_OUTPUT in script, step_id
        assert "--build-arg SOURCE_DATE_EPOCH=${_SOURCE_DATE_EPOCH}" in script, step_id
        assert not re.search(r"--push\b", script), step_id
        assert "--provenance=false" in script, step_id
