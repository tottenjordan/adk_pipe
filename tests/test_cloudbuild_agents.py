"""Cloud Build engine deploys: deployment/cloudbuild.agents.yaml + its helpers.

Structural checks only (no build is submitted): the config parses, every
selectable agent has its own parallel deploy step, the deploy profile
(deployment/deploy.env) is explicit and safe to commit, the clean-env wrapper
refuses stale engine IDs, and engine_handoff.py merges/applies the new IDs.
"""

import json
import os
import subprocess
import types
from unittest.mock import MagicMock

import dotenv
import pytest
import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG = os.path.join(ROOT, "deployment", "cloudbuild.agents.yaml")
PROFILE = os.path.join(ROOT, "deployment", "deploy.env")
WRAPPER = os.path.join(ROOT, "deployment", "cloudbuild_env.sh")


def _da():
    try:
        import deployment.deploy_agent as deploy_agent
    except ImportError as e:  # agentplatform/absl missing in a bare env
        pytest.skip(f"deploy_agent import unavailable: {e}")
    return deploy_agent


def _handoff():
    try:
        import deployment.engine_handoff as engine_handoff
    except ImportError as e:
        pytest.skip(f"engine_handoff import unavailable: {e}")
    return engine_handoff


@pytest.fixture(scope="module")
def build():
    with open(CONFIG) as f:
        return yaml.safe_load(f)


def _steps(build):
    return {s["id"]: s for s in build["steps"]}


def _script(step):
    return "\n".join(step.get("args", []))


# --- cloudbuild.agents.yaml -------------------------------------------------
class TestBuildConfig:
    def test_default_machine(self, build):
        """No ``machineType``: the default machine starts in ~1 s, E2_HIGHCPU_8 queued
        ~50 s for a worker on every engine deploy (measured 2026-10-12). The local work
        (``uv sync``, staging + cloudpickle + a small tarball) is light; the deploy
        steps mostly wait on Vertex Agent Engine, so extra CPUs buy nothing."""
        assert "machineType" not in build.get("options", {})

    def test_step_ids_unique(self, build):
        ids = [s["id"] for s in build["steps"]]
        assert len(ids) == len(set(ids))

    def test_wait_for_references_existing_steps(self, build):
        ids = {s["id"] for s in build["steps"]}
        for step in build["steps"]:
            for dep in step.get("waitFor", []):
                assert dep == "-" or dep in ids, (step["id"], dep)

    def test_one_parallel_deploy_step_per_agent(self, build):
        da = _da()
        steps = _steps(build)
        for agent in da.AGENT_NAMES:
            step = steps[f"deploy-{agent}"]
            # All deploy steps wait only on the Python check, so they run in parallel.
            assert step["waitFor"] == ["python-313"]
            script = _script(step)
            assert f"--agent={agent}" in script
            assert "--create" in script
            assert "--nowrite_env" in script
            assert f"--output_json=/workspace/engine_ids/{agent}.json" in script
            assert "deployment/cloudbuild_env.sh" in script
            # Collect still runs (and fails the build) when one deploy fails.
            assert step.get("allowFailure") is True

    def test_default_agents_cover_every_deployable_agent(self, build):
        da = _da()
        agents = build["substitutions"]["_AGENTS"].split(",")
        assert sorted(agents) == sorted(da.AGENT_NAMES)

    def test_versions_are_per_agent_substitutions(self, build):
        subs = build["substitutions"]
        for key in ("_CREATIVE_VERSION", "_INTERACTIVE_VERSION", "_SCOUT_VERSION"):
            assert key in subs
        steps = _steps(build)
        assert "${_CREATIVE_VERSION}" in _script(steps["deploy-creative_agent"])
        assert "${_INTERACTIVE_VERSION}" in _script(
            steps["deploy-interactive_creative"]
        )
        assert "${_SCOUT_VERSION}" in _script(steps["deploy-trend_scout"])

    def test_python_steps_use_pinned_uv_python313_image(self, build):
        image = build["substitutions"]["_UV_IMAGE"]
        assert image.startswith("ghcr.io/astral-sh/uv:")
        assert "python3.13" in image
        assert "@sha256:" in image  # tag + digest pinned
        for step in build["steps"]:
            if step["id"] != "clone":
                assert step["name"] == "${_UV_IMAGE}", step["id"]

    def test_fresh_clone_then_frozen_no_dev_sync(self, build):
        steps = _steps(build)
        clone = _script(steps["clone"])
        assert "git fetch" in clone and "${_REF}" in clone
        assert "uv sync --frozen --no-dev" in _script(steps["sync"])

    def test_python_version_is_asserted(self, build):
        script = _script(_steps(build)["python-313"])
        assert "(3, 13)" in script
        assert ".venv/bin/python" in script

    def test_collect_and_smoke(self, build):
        steps = _steps(build)
        collect = steps["collect"]
        assert sorted(collect["waitFor"]) == sorted(
            f"deploy-{a}" for a in _da().AGENT_NAMES
        )
        assert "/workspace/engine_ids.json" in _script(collect)
        smoke = steps["smoke"]
        assert smoke["waitFor"] == ["collect"]
        script = _script(smoke)
        assert "${_SMOKE}" in script
        assert "CREATIVE_AGENT_ENGINE_ID=" in script
        assert "--check smoke --agent creative_agent" in script

    def test_bash_vars_are_escaped_from_substitution(self, build):
        # Cloud Build substitutes $NAME / ${NAME}; bash variables must be $$-escaped,
        # so every single-$ reference is a substitution or a built-in.
        import re

        allowed = set(build["substitutions"]) | {"PROJECT_ID", "BUILD_ID"}
        for step in build["steps"]:
            text = _script(step).replace("$$", "")
            for m in re.finditer(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", text):
                assert m.group(1) in allowed, (step["id"], m.group(0))

    def test_logging_and_loose_substitutions(self, build):
        assert build["options"]["logging"] == "CLOUD_LOGGING_ONLY"
        assert build["options"]["substitutionOption"] == "ALLOW_LOOSE"


# --- deployment/deploy.env ----------------------------------------------------
class TestDeployProfile:
    def test_covers_every_shipped_env_var(self):
        da = _da()
        profile = dotenv.dotenv_values(PROFILE)
        missing = [k for k in da.ENV_VAR_DICT if k not in profile]
        assert not missing, f"deploy.env misses ENV_VAR_DICT keys: {missing}"

    def test_has_deploy_inputs_and_pickle_time_knobs(self):
        profile = dotenv.dotenv_values(PROFILE)
        for key in (
            "GOOGLE_CLOUD_PROJECT",
            "GCP_REGION",
            "GOOGLE_CLOUD_LOCATION",
            "CRITIC_FALLBACK_MODEL",
            "MODEL_REQUEST_TIMEOUT_SECONDS",
            "MODEL_ARMOR_TEMPLATE",
        ):
            assert key in profile, key
        assert profile["GOOGLE_CLOUD_LOCATION"] == "global"
        assert profile["GCP_REGION"] == "us-central1"
        # Empty would silently disable Pro -> flash failover.
        assert profile["CRITIC_FALLBACK_MODEL"]

    def test_no_engine_ids_or_legacy_bucket_vars(self):
        profile = dotenv.dotenv_values(PROFILE)
        bad = [
            k
            for k in profile
            if k.endswith("_AGENT_ENGINE_ID") or k in {"BUCKET", "GCS_BUCKET_NAME"}
        ]
        assert not bad

    def test_no_secret_looking_keys(self):
        profile = dotenv.dotenv_values(PROFILE)
        bad = [
            k
            for k in profile
            if any(w in k for w in ("SECRET", "TOKEN", "PASSWORD", "API_KEY"))
        ]
        assert not bad

    def test_bash_sourcing_matches_dotenv(self):
        # The build sources the profile with bash (set -a; . file); python-dotenv
        # must read the same values, or local tooling and the build would differ.
        out = subprocess.run(
            ["env", "-i", "/bin/bash", "-c", 'set -a; . "$0"; exec env -0', PROFILE],
            capture_output=True,
            check=True,
        ).stdout.decode()
        sourced = dict(kv.split("=", 1) for kv in out.split("\0") if "=" in kv)
        for key, value in dotenv.dotenv_values(PROFILE).items():
            assert sourced.get(key) == (value or ""), key


# --- deployment/cloudbuild_env.sh -----------------------------------------------
def _run_wrapper(profile, project, *cmd, extra_env=None):
    env = {"PATH": os.environ["PATH"], **(extra_env or {})}
    return subprocess.run(
        ["bash", WRAPPER, str(profile), project, "--", *cmd],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )


class TestCleanEnvWrapper:
    def test_runs_command_with_only_the_profile_env(self, tmp_path):
        profile = tmp_path / "p.env"
        profile.write_text("GOOGLE_CLOUD_PROJECT=proj\nFOO='a b'\n")
        r = _run_wrapper(
            profile,
            "proj",
            "bash",
            "-c",
            'echo "$FOO|${LEAK-unset}|${SCOUT_AGENT_ENGINE_ID-unset}"',
            extra_env={"LEAK": "x", "SCOUT_AGENT_ENGINE_ID": "stale"},
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "a b|unset|unset"

    def test_refuses_profile_for_another_project(self, tmp_path):
        profile = tmp_path / "p.env"
        profile.write_text("GOOGLE_CLOUD_PROJECT=proj\n")
        r = _run_wrapper(profile, "other", "true")
        assert r.returncode != 0
        assert "other" in r.stderr

    def test_refuses_engine_ids_in_profile(self, tmp_path):
        profile = tmp_path / "p.env"
        profile.write_text("GOOGLE_CLOUD_PROJECT=proj\nCREATIVE_AGENT_ENGINE_ID=x\n")
        r = _run_wrapper(profile, "proj", "true")
        assert r.returncode != 0
        assert "CREATIVE_AGENT_ENGINE_ID" in r.stderr

    def test_committed_profile_passes(self):
        project = dotenv.dotenv_values(PROFILE)["GOOGLE_CLOUD_PROJECT"]
        r = _run_wrapper(PROFILE, project, "true")
        assert r.returncode == 0, r.stderr


# --- deployment/engine_handoff.py ----------------------------------------------
RES = "projects/1/locations/us-central1/reasoningEngines/{}"


def _write_record(dir_, agent, rid, version="v9"):
    da = _da()
    da.write_engine_record(
        str(dir_ / f"{agent}.json"), agent, version, RES.format(rid), True
    )


def _runtime(rid, display_name, create_time):
    return types.SimpleNamespace(
        api_resource=types.SimpleNamespace(
            name=RES.format(rid), display_name=display_name, create_time=create_time
        )
    )


class TestEngineHandoff:
    def test_collect_merges_records_and_reports_missing(self, tmp_path, capsys):
        eh = _handoff()
        _write_record(tmp_path, "creative_agent", 11)
        _write_record(tmp_path, "trend_scout", 33)
        out = tmp_path / "engine_ids.json"
        doc, missing = eh.collect(
            str(tmp_path),
            str(out),
            expected=["creative_agent", "interactive_creative", "trend_scout"],
            commit="abc123",
        )
        assert missing == ["interactive_creative"]
        assert json.loads(out.read_text()) == doc
        assert doc["commit"] == "abc123"
        assert doc["engines"]["creative_agent"]["resource_name"] == RES.format(11)
        assert "interactive_creative" not in doc["engines"]
        printed = capsys.readouterr().out
        assert f"CREATIVE_AGENT_ENGINE_ID='{RES.format(11)}'" in printed

    def test_previous_engine_is_newest_other_with_same_prefix(self):
        eh = _handoff()
        runtimes = [
            _runtime(1, "creative-trend-agent-v34", "2026-10-01T00:00:00Z"),
            _runtime(2, "creative-trend-agent-v35", "2026-10-05T00:00:00Z"),
            _runtime(3, "creative-trend-agent-v36", "2026-10-12T00:00:00Z"),  # new
            _runtime(4, "interactive-creative-agent-v9", "2026-10-11T00:00:00Z"),
        ]
        assert eh.previous_engine(
            runtimes, "creative_agent", exclude=RES.format(3)
        ) == RES.format(2)
        assert eh.previous_engine(runtimes, "trend_scout", exclude="") is None

    def test_get_prints_one_resource_name(self, tmp_path):
        eh = _handoff()
        _write_record(tmp_path, "creative_agent", 11)
        out = tmp_path / "engine_ids.json"
        eh.collect(str(tmp_path), str(out), expected=["creative_agent"])
        assert eh.get(str(out), "creative_agent") == RES.format(11)
        with pytest.raises(KeyError):
            eh.get(str(out), "trend_scout")

    def test_apply_updates_env_and_reports_old_to_new(self, tmp_path, capsys):
        eh = _handoff()
        _write_record(tmp_path, "creative_agent", 11)
        ids = tmp_path / "engine_ids.json"
        eh.collect(str(tmp_path), str(ids), expected=["creative_agent"])
        env = tmp_path / ".env"
        env.write_text(f"CREATIVE_AGENT_ENGINE_ID='{RES.format(1)}'\nKEEP=1\n")
        eh.apply(str(ids), str(env))
        values = dotenv.dotenv_values(env)
        assert values["CREATIVE_AGENT_ENGINE_ID"] == RES.format(11)
        assert values["KEEP"] == "1"
        assert f"{RES.format(1)} -> {RES.format(11)}" in capsys.readouterr().out

    def test_upload_uses_storage_client(self, tmp_path, monkeypatch):
        eh = _handoff()
        client = MagicMock()
        monkeypatch.setattr(eh, "_storage_client", lambda: client)
        path = tmp_path / "engine_ids.json"
        path.write_text("{}")
        eh.upload(str(path), "gs://bkt/adk-pipe/cloudbuild/b1/engine_ids.json")
        client.bucket.assert_called_once_with("bkt")
        client.bucket.return_value.blob.assert_called_once_with(
            "adk-pipe/cloudbuild/b1/engine_ids.json"
        )
        client.bucket.return_value.blob.return_value.upload_from_filename.assert_called_once_with(
            str(path), content_type="application/json"
        )

    def test_show_prints_old_to_new(self, tmp_path, capsys):
        eh = _handoff()
        _write_record(tmp_path, "creative_agent", 11)
        out = tmp_path / "engine_ids.json"
        runtimes = [
            _runtime(1, "creative-trend-agent-v1", "2026-10-01T00:00:00Z"),
            _runtime(11, "creative-trend-agent-v9", "2026-10-12T00:00:00Z"),
        ]
        eh.collect(str(tmp_path), str(out), ["creative_agent"], runtimes=runtimes)
        capsys.readouterr()
        assert eh.main(["show", "--ids", str(out)]) == 0
        assert (
            capsys.readouterr().out.strip()
            == f"creative_agent: {RES.format(1)} -> {RES.format(11)}"
        )

    def test_cli_collect_exits_nonzero_when_an_agent_is_missing(self, tmp_path):
        eh = _handoff()
        _write_record(tmp_path, "creative_agent", 11)
        out = tmp_path / "engine_ids.json"
        argv = ["collect", "--dir", str(tmp_path), "--out", str(out), "--expect"]
        assert eh.main([*argv, "creative_agent"]) == 0
        assert eh.main([*argv, "creative_agent,trend_scout"]) == 1
