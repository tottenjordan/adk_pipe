"""Bandit image build: one image (endpoint + traffic job), dependency layer cached."""

from pathlib import Path

from tests.repo_layout import BANDIT_DIRS, REPO_ROOT

DOCKERFILE = BANDIT_DIRS["bandit_serving"] / "Dockerfile"
DEPLOY_JOB = REPO_ROOT / "deployment" / "bandit" / "deploy_traffic_job.sh"


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


def test_only_one_bandit_dockerfile():
    assert sorted(p for d in BANDIT_DIRS.values() for p in d.glob("Dockerfile")) == [
        DOCKERFILE
    ]


def test_dependencies_install_before_code():
    ins = _instructions(DOCKERFILE)
    installs = [i for i, (k, a) in enumerate(ins) if k == "RUN" and "pip install" in a]
    assert len(installs) == 1, installs
    copies = [(i, a) for i, (k, a) in enumerate(ins) if k == "COPY"]
    before = [a for i, a in copies if i < installs[0] and "--from" not in a]
    assert len(before) == 1 and "requirements.txt" in before[0], before
    code = [i for i, a in copies if "--from" not in a and "requirements.txt" not in a]
    assert code and min(code) > installs[0]
    assert {ins[i][1].split()[0] for i in code} == {
        "bandit_stack/bandit/",
        "bandit_stack/bandit_serving/*.py",
        "bandit_stack/bandit_traffic/*.py",
    }


def test_multi_stage_uv_venv_non_root():
    ins = _instructions(DOCKERFILE)
    froms = [a for k, a in ins if k == "FROM"]
    assert len(froms) == 3, froms  # uv, builder, final
    assert "uv venv /opt/venv" in next(a for k, a in ins if k == "RUN" and "uv " in a)
    assert ("COPY", "--from=builder /opt/venv /opt/venv") in ins
    last_from = max(i for i, (k, _) in enumerate(ins) if k == "FROM")
    users = [a for k, a in ins[last_from:] if k == "USER"]
    assert users and users[-1] not in ("root", "0"), users


def test_default_entrypoint_is_the_cpr_model_server():
    """Vertex Model.upload uses the image's default entrypoint, so the model
    server stays the default; the traffic job overrides the command."""
    ins = _instructions(DOCKERFILE)
    assert ins[-1] == (
        "ENTRYPOINT",
        '["python", "-m", "google.cloud.aiplatform.prediction.model_server"]',
    )


CLOUDBUILD = REPO_ROOT / "deployment" / "bandit" / "cloudbuild.bandit.yaml"
IGNORE = REPO_ROOT / "deployment" / "bandit" / "bandit.gcloudignore"


def _cloudbuild() -> dict:
    import yaml

    return yaml.safe_load(CLOUDBUILD.read_text())


def _steps() -> dict:
    return {s["id"]: s for s in _cloudbuild()["steps"]}


def test_one_build_step_after_buildx_setup():
    steps = _steps()
    assert list(steps) == ["buildx-setup", "build-bandit"]
    assert "waitFor" not in steps["buildx-setup"]
    assert steps["build-bandit"]["waitFor"] == ["buildx-setup"]


def test_build_step_uses_registry_cache_and_prints_digest():
    script = _steps()["build-bandit"]["args"][-1]
    assert "--file bandit_stack/bandit_serving/Dockerfile" in script
    assert "--cache-from type=registry,ref=${_BANDIT_IMAGE}:buildcache" in script
    assert "--cache-to type=registry,ref=${_BANDIT_IMAGE}:buildcache,mode=max" in script
    assert "--tag ${_BANDIT_IMAGE}:${SHORT_SHA}" in script
    assert "--tag ${_BANDIT_IMAGE}:latest" in script
    assert "--push" in script and "--metadata-file" in script
    assert 'echo "BANDIT_IMAGE=${_BANDIT_IMAGE}@$$(cat' in script
    assert script.rstrip().split("\n")[-2].strip().startswith("test -s")
    text = CLOUDBUILD.read_text()
    assert "CPR_IMAGE" not in text and "TRAFFIC_IMAGE" not in text


def test_substitution_defaults_and_options():
    cfg = _cloudbuild()
    repo = "us-central1-docker.pkg.dev/${PROJECT_ID}/cpr"
    assert cfg["substitutions"] == {"_BANDIT_IMAGE": f"{repo}/trend-trawler-bandit"}
    assert cfg["options"]["dynamicSubstitutions"] is True
    assert cfg["options"]["logging"] == "CLOUD_LOGGING_ONLY"


def test_traffic_job_runs_the_bandit_image_with_a_command_override():
    deploy = DEPLOY_JOB.read_text()
    assert "cpr/trend-trawler-bandit:${IMAGE_TAG}" in deploy
    assert "trend-trawler-bandit-traffic:" not in deploy  # the retired image
    # deploy by digest (argument or IMAGE); the tag above is only a warned
    # fallback (behaviour: tests/test_bandit_deploy_traffic_job.py)
    assert 'IMAGE="${1:-${IMAGE:-}}"' in deploy
    assert "--command python" in deploy
    assert "--args=-m,bandit_traffic.main" in deploy
    # the job name is unchanged (the api launches it by name)
    assert "${BANDIT_TRAFFIC_JOB:-trend-trawler-bandit-traffic}" in deploy


def test_ignore_file_uploads_only_the_bandit_sources():
    lines = [
        ln.strip()
        for ln in IGNORE.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    assert lines[0] == "/*"
    allowed = {ln for ln in lines if ln.startswith("!")}
    assert allowed == {"!/bandit_stack/"}
    assert not any("frontend" in ln or ".env" in ln for ln in allowed)
