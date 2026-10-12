"""Bandit image builds: both Dockerfiles cache their dependency layer."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = {
    "cpr": REPO_ROOT / "bandit_serving" / "Dockerfile",
    "traffic": REPO_ROOT / "bandit_traffic" / "Dockerfile",
}


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


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_dependencies_install_before_code(name):
    ins = _instructions(DOCKERFILES[name])
    installs = [i for i, (k, a) in enumerate(ins) if k == "RUN" and "pip install" in a]
    assert len(installs) == 1, installs
    copies = [(i, a) for i, (k, a) in enumerate(ins) if k == "COPY"]
    before = [a for i, a in copies if i < installs[0] and "--from" not in a]
    assert len(before) == 1 and "requirements.txt" in before[0], before
    code = [i for i, a in copies if "--from" not in a and "requirements.txt" not in a]
    assert code and min(code) > installs[0]


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_multi_stage_uv_venv_non_root(name):
    ins = _instructions(DOCKERFILES[name])
    froms = [a for k, a in ins if k == "FROM"]
    assert len(froms) == 3, froms  # uv, builder, final
    assert "uv venv /opt/venv" in next(a for k, a in ins if k == "RUN" and "uv " in a)
    assert ("COPY", "--from=builder /opt/venv /opt/venv") in ins
    last_from = max(i for i, (k, _) in enumerate(ins) if k == "FROM")
    users = [a for k, a in ins[last_from:] if k == "USER"]
    assert users and users[-1] not in ("root", "0"), users


def test_traffic_entrypoint_unchanged():
    ins = _instructions(DOCKERFILES["traffic"])
    assert ins[-1] == ("ENTRYPOINT", '["python", "-m", "bandit_traffic.main"]')
    assert ("USER", "app") in ins


CLOUDBUILD = REPO_ROOT / "deployment" / "bandit" / "cloudbuild.bandit.yaml"
IGNORE = REPO_ROOT / "deployment" / "bandit" / "bandit.gcloudignore"
BUILD_STEPS = {
    "build-cpr": ("bandit_serving/Dockerfile", "_CPR_IMAGE", "CPR_IMAGE"),
    "build-traffic": ("bandit_traffic/Dockerfile", "_TRAFFIC_IMAGE", "TRAFFIC_IMAGE"),
}


def _cloudbuild() -> dict:
    import yaml

    return yaml.safe_load(CLOUDBUILD.read_text())


def _steps() -> dict:
    return {s["id"]: s for s in _cloudbuild()["steps"]}


def test_build_steps_run_in_parallel_after_buildx_setup():
    steps = _steps()
    assert "buildx-setup" in steps and "waitFor" not in steps["buildx-setup"]
    for step_id in BUILD_STEPS:
        assert steps[step_id]["waitFor"] == ["buildx-setup"], step_id


@pytest.mark.parametrize("step_id", sorted(BUILD_STEPS))
def test_build_step_uses_registry_cache_and_prints_digest(step_id):
    dockerfile, image_var, digest_name = BUILD_STEPS[step_id]
    script = _steps()[step_id]["args"][-1]
    assert f"--file {dockerfile}" in script
    assert f"--cache-from type=registry,ref=${{{image_var}}}:buildcache" in script
    assert f"--cache-to type=registry,ref=${{{image_var}}}:buildcache,mode=max" in script
    assert f"--tag ${{{image_var}}}:${{SHORT_SHA}}" in script
    assert f"--tag ${{{image_var}}}:latest" in script
    assert "--push" in script and "--metadata-file" in script
    assert f'echo "{digest_name}=${{{image_var}}}@$$(cat' in script
    assert script.rstrip().split("\n")[-2].strip().startswith("test -s")


def test_substitution_defaults_and_options():
    cfg = _cloudbuild()
    subs = cfg["substitutions"]
    repo = "us-central1-docker.pkg.dev/${PROJECT_ID}/cpr"
    assert subs["_CPR_IMAGE"] == f"{repo}/trend-trawler-bandit"
    assert subs["_TRAFFIC_IMAGE"] == f"{repo}/trend-trawler-bandit-traffic"
    assert cfg["options"]["dynamicSubstitutions"] is True
    assert cfg["options"]["logging"] == "CLOUD_LOGGING_ONLY"
    # the traffic image path + tag scheme deploy_traffic_job.sh resolves
    deploy = (REPO_ROOT / "deployment" / "bandit" / "deploy_traffic_job.sh").read_text()
    assert "cpr/trend-trawler-bandit-traffic:${IMAGE_TAG}" in deploy


def test_ignore_file_uploads_only_the_bandit_sources():
    lines = [
        ln.strip()
        for ln in IGNORE.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    assert lines[0] == "/*"
    allowed = {ln for ln in lines if ln.startswith("!")}
    assert allowed == {"!/bandit/", "!/bandit_serving/", "!/bandit_traffic/"}
    assert not any("frontend" in ln or ".env" in ln for ln in allowed)
