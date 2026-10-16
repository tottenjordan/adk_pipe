"""CRF container image: one Dockerfile serves both Cloud Run functions.

``creative-trawler-crf`` (orchestrator) and ``creative-worker-crf`` (worker) run the
same image; ``FUNCTION_TARGET`` (set per service) picks the entry point and
``FUNCTION_SIGNATURE_TYPE`` (default ``cloudevent``; ``http`` for the Cloud Tasks
worker) its signature. Named so
the ``tests/test_crf_*.py`` glob of the CRF-deps CI venv (no pyyaml) skips it.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

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
    # The hashed lock, never the loose requirements.txt (C15).
    assert "--require-hashes -r /build/requirements.lock.txt" in ins[installs[0]][1]
    copies = [(i, a) for i, (k, a) in enumerate(ins) if k == "COPY"]
    before = [a for i, a in copies if i < installs[0] and "--from" not in a]
    assert before == [
        "cloud_functions/creative_fanout/requirements.lock.txt"
        " /build/requirements.lock.txt"
    ]
    code = [i for i, a in copies if "--from" not in a and "requirements" not in a]
    assert code and min(code) > installs[0]


def test_base_image_is_one_literal_digest_pinned_from():
    """Dependabot skips ``FROM ${ARG}``: the base is one literal tag + digest
    FROM, and the builder and final stages both build on it."""
    ins = _instructions(DOCKERFILE)
    assert not [a for k, a in ins if k == "ARG" and a.startswith("BASE_IMAGE")]
    froms = [a for k, a in ins if k == "FROM"]
    bases = [a for a in froms if a.startswith("python:")]
    assert len(bases) == 1, froms
    assert re.fullmatch(r"python:3\.13\.\d+-slim@sha256:[0-9a-f]{64} AS base", bases[0])
    assert froms[-2:] == ["base AS builder", "base"], froms


def test_lock_pins_every_requirement_with_hashes():
    """Every requirements.txt pin is in the lock at the same version, and every
    locked package carries hashes (``--require-hashes`` needs both)."""
    lock = (CRF_DIR / "requirements.lock.txt").read_text()
    entries = re.findall(r"^([A-Za-z0-9_.\-]+)==(\S+)", lock, re.MULTILINE)
    assert entries
    locked = {name.lower().replace("_", "-"): ver for name, ver in entries}
    for line in (CRF_DIR / "requirements.txt").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, _, ver = line.partition("==")
        name = name.split("[")[0].lower().replace("_", "-")
        assert locked.get(name) == ver.strip(), (name, locked.get(name))
    blocks = re.split(r"^(?=[A-Za-z0-9])", lock, flags=re.MULTILINE)
    for block in blocks:
        if "==" in block.split("\n", 1)[0]:
            assert "--hash=sha256:" in block, block.split("\n", 1)[0]


def test_multi_stage_uv_venv_non_root():
    ins = _instructions(DOCKERFILE)
    froms = [a for k, a in ins if k == "FROM"]
    assert len(froms) == 4, froms  # uv, base, builder, final
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
    # The signature comes from the service env, cloudevent by default.
    assert '--signature-type=\\"${FUNCTION_SIGNATURE_TYPE:-cloudevent}\\"' in cmd, cmd
    assert "${PORT:-8080}" in cmd


def _start_script() -> str:
    """The CMD's ``sh -c`` script."""
    (cmd,) = [a for k, a in _instructions(DOCKERFILE) if k == "CMD"]
    argv = json.loads(cmd)
    assert argv[:2] == ["/bin/sh", "-c"]
    return argv[2]


@pytest.mark.parametrize(
    "env, expected",
    [
        (
            {"FUNCTION_TARGET": "crf_entrypoint"},
            "--target=crf_entrypoint --signature-type=cloudevent",
        ),
        (
            {"FUNCTION_TARGET": "agent_worker_task", "FUNCTION_SIGNATURE_TYPE": "http"},
            "--target=agent_worker_task --signature-type=http",
        ),
    ],
)
def test_start_command_expands_target_and_signature(env, expected):
    """Run the CMD's script with ``exec`` swapped for ``echo``."""
    script = _start_script().replace("exec functions-framework", "echo ff", 1)
    out = subprocess.run(
        ["/bin/sh", "-c", script], env=env, capture_output=True, text=True, check=True
    ).stdout
    assert expected in out
    assert "--port=8080" in out


def test_start_command_exits_without_a_target():
    script = _start_script().replace("exec functions-framework", "echo ff", 1)
    result = subprocess.run(
        ["/bin/sh", "-c", script], env={}, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "FUNCTION_TARGET is not set" in result.stderr


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
        "cloud_functions/creative_fanout/clients.py",
        "cloud_functions/creative_fanout/completion.py",
        "cloud_functions/creative_fanout/config.py",
        "cloud_functions/creative_fanout/logs.py",
        "cloud_functions/creative_fanout/main.py",
        "cloud_functions/creative_fanout/messages.py",
        "cloud_functions/creative_fanout/orchestrator.py",
        "cloud_functions/creative_fanout/session.py",
        "cloud_functions/creative_fanout/status_store.py",
        "cloud_functions/creative_fanout/worker.py",
        "cloud_functions/creative_fanout/requirements.lock.txt",
    }
    shipped = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in CRF_DIR.iterdir()
        if p.suffix == ".py" or p.name == "requirements.lock.txt"
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


def _deploy_cmd(deploy: str, service: str) -> str:
    return deploy.split(f"gcloud run deploy {service} ", 1)[1].split("gcloud", 1)[0]


def test_optional_deploy_rolls_both_crf_services_without_traffic():
    build = _root_build()
    step = _step(build, "deploy")
    assert "build-crf" in step["waitFor"]
    script = step["args"][-1]
    skip, _, deploy = script.partition("exit 0")
    assert "CRF_IMAGE=" in skip  # digests printed on the default (no-deploy) path
    for service in ("creative-trawler-crf", "creative-worker-crf"):
        cmd = _deploy_cmd(deploy, service)
        assert '--image "${_CRF_IMAGE}@$$(cat /workspace/crf.digest)"' in cmd
        assert "--no-traffic" in cmd
        assert "--clear-base-image" in cmd
        # Never --source/--function (buildpacks) or env replacement/removal.
        assert not re.search(
            r"--source|--function|--set-env-vars|--remove-env-vars|--clear-env-vars"
            r"|--env-vars-file",
            cmd,
        ), cmd
    assert "update-traffic" not in deploy


def test_optional_deploy_sets_only_the_orchestrator_target():
    """The orchestrator's target never changes, so the step re-asserts it. The
    worker's target and signature belong to the service (agent_worker_task +
    http behind Cloud Tasks, the only transport): the step must not overwrite
    either."""
    _, _, deploy = _step(_root_build(), "deploy")["args"][-1].partition("exit 0")
    orchestrator = _deploy_cmd(deploy, "creative-trawler-crf")
    assert "--update-env-vars FUNCTION_TARGET=crf_entrypoint" in orchestrator
    assert "FUNCTION_SIGNATURE_TYPE" not in orchestrator  # cloudevent default
    worker = _deploy_cmd(deploy, "creative-worker-crf")
    assert "FUNCTION_TARGET" not in worker
    assert "FUNCTION_SIGNATURE_TYPE" not in worker
    assert "--update-env-vars" not in worker


def test_optional_deploy_rolls_api_and_web_by_digest_without_traffic():
    build = _root_build()
    step = _step(build, "deploy")
    assert {"build-api", "build-web"} <= set(step["waitFor"])
    script = step["args"][-1]
    _, _, deploy = script.partition("exit 0")
    for service, image, digest in (
        ("trend-trawler-api", "_API_IMAGE", "api"),
        ("trend-trawler-web", "_WEB_IMAGE", "web"),
    ):
        cmd = deploy.split(f"gcloud run deploy {service} ", 1)[1].split("gcloud", 1)[0]
        assert f'--image "${{{image}}}@$$(cat /workspace/{digest}.digest)"' in cmd
        assert "--no-traffic" in cmd
        # Never public (only the share service is), never wipe env, never rebuild.
        assert not re.search(r"--allow-unauthenticated|--set-env-vars|--source", cmd), (
            cmd
        )
        if service == "trend-trawler-api":
            # Detached /runs need CPU outside requests and no scale-to-zero.
            assert "--no-cpu-throttling" in cmd
            assert "--min-instances 1" in cmd
    assert "update-traffic" not in deploy


def test_upload_filter_keeps_only_the_crf_function_dir():
    lines = (REPO_ROOT / "deployment" / "cloudbuild.gcloudignore").read_text()
    lines = [ln.strip() for ln in lines.splitlines()]
    assert "/cloud_functions/*" in lines
    assert "!/cloud_functions/creative_fanout/" in lines
    # Re-excluded inside, then only the build's files (never a local message.json).
    assert "/cloud_functions/creative_fanout/*" in lines
    assert "!/cloud_functions/creative_fanout/*.py" in lines
    assert "!/cloud_functions/creative_fanout/requirements.lock.txt" in lines
    assert "/cloud_functions/" not in lines
    assert "__pycache__/" in lines
