"""runserver/experiments_deploy.py + experiments_jobs.py with fake SDKs (no GCP)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from runserver.experiments_deploy import FakeDeployer, VertexDeployer
from runserver.experiments_jobs import (
    CloudRunJobsRunner,
    FakeJobsRunner,
    build_env_overrides,
    execution_state,
)


class _FakeLib:
    """Stands in for deployment.bandit.endpoint."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.deployed: list[dict] = []

    def experiment_labels(self, eid):
        return {"app": "trend-trawler", "experiment": eid}

    def display_name_for(self, eid, kind):
        return f"{kind}-{eid}"

    def upload_model(self, image, artifact, name, labels):
        self.calls.append(("upload", image, artifact, name, labels))
        return SimpleNamespace(resource_name="models/1")

    def create_endpoint(self, name, labels):
        self.calls.append(("endpoint", name, labels))
        return SimpleNamespace(resource_name="endpoints/1")

    def endpoint_state(self, endpoint):
        self.calls.append(("state", endpoint))
        return {"exists": True, "deployed_models": list(self.deployed)}

    def get_model(self, name):
        return SimpleNamespace(resource_name=name)

    def get_endpoint(self, name):
        return SimpleNamespace(resource_name=name)

    def deploy_model(self, model, endpoint, machine_type, sa):
        self.calls.append(("deploy", model.resource_name, endpoint.resource_name, sa))
        self.deployed.append({"id": "dm-9", "model": model.resource_name})
        return "dm-9"

    def undeploy_and_delete(self, endpoint, model):
        self.calls.append(("teardown", endpoint, model))


def test_vertex_deployer_steps_and_reports_ids():
    lib = _FakeLib()
    d = VertexDeployer("img:1", service_account="sa@x", lib=lib)
    steps: list[dict] = []

    async def on_step(ids):
        steps.append(ids)

    ids = asyncio.run(d.deploy("e1", "gs://b/bandit/e1", on_step=on_step))
    assert ids == {
        "model_resource": "models/1",
        "endpoint_id": "endpoints/1",
        "deployed_model_id": "dm-9",
    }
    assert steps == [
        {"model_resource": "models/1"},
        {"endpoint_id": "endpoints/1"},
        {"deployed_model_id": "dm-9"},
    ]
    assert lib.calls[0] == (
        "upload",
        "img:1",
        "gs://b/bandit/e1",
        "model-e1",
        {"app": "trend-trawler", "experiment": "e1"},
    )
    assert ("deploy", "models/1", "endpoints/1", "sa@x") in lib.calls


def test_vertex_deployer_resume_skips_existing_and_adopts_deployment():
    lib = _FakeLib()
    lib.deployed = [{"id": "dm-old", "model": "models/1"}]
    d = VertexDeployer("img:1", lib=lib)
    ids = asyncio.run(
        d.deploy(
            "e1",
            "gs://x",
            existing={"model_resource": "models/1", "endpoint_id": "endpoints/1"},
        )
    )
    assert ids["deployed_model_id"] == "dm-old"
    assert [c[0] for c in lib.calls] == ["state"]  # no upload/create/deploy


def test_vertex_deployer_requires_image_and_teardown_state():
    lib = _FakeLib()
    with pytest.raises(RuntimeError, match="BANDIT_SERVING_IMAGE"):
        asyncio.run(VertexDeployer("", lib=lib).deploy("e1", "gs://x"))
    d = VertexDeployer("img", lib=lib)
    asyncio.run(d.teardown({"endpoint_id": "endpoints/1", "model_resource": "m"}))
    asyncio.run(d.teardown({}))  # nothing recorded: no SDK call
    assert [c for c in lib.calls if c[0] == "teardown"] == [
        ("teardown", "endpoints/1", "m")
    ]
    assert asyncio.run(d.state({})) == {"exists": False, "deployed": False}
    assert asyncio.run(d.state({"endpoint_id": "endpoints/1"})) == {
        "exists": True,
        "deployed": False,
    }


def test_fake_deployer_failure_and_drop():
    d = FakeDeployer(fail_at="endpoint_id")
    seen: list[dict] = []

    async def on_step(ids):
        seen.append(ids)

    with pytest.raises(RuntimeError):
        asyncio.run(d.deploy("e", "gs://x", on_step=on_step))
    assert list(seen[0]) == ["model_resource"]
    ok = FakeDeployer()
    ids = asyncio.run(ok.deploy("e", "gs://x"))
    row = {**ids}
    assert asyncio.run(ok.state(row)) == {"exists": True, "deployed": True}
    ok.drop(ids["endpoint_id"])
    assert asyncio.run(ok.state(row))["exists"] is False


def test_env_overrides_and_execution_state():
    env = build_env_overrides(
        experiment_id="e1",
        config_uri="gs://c",
        endpoint_id="endpoints/1",
        episodes=5,
        horizon=None,
    )
    assert env == [
        {"name": "EXPERIMENT_ID", "value": "e1"},
        {"name": "CONFIG_URI", "value": "gs://c"},
        {"name": "ENDPOINT_ID", "value": "endpoints/1"},
        {"name": "EPISODES", "value": "5"},
    ]
    assert execution_state(SimpleNamespace(completion_time=None)) == "running"
    done = SimpleNamespace(completion_time=1, failed_count=0, cancelled_count=0)
    assert execution_state(done) == "succeeded"
    assert (
        execution_state(SimpleNamespace(completion_time=1, failed_count=1)) == "failed"
    )


def test_cloud_run_jobs_runner_passes_overrides():
    sent = {}

    class _Jobs:
        def run_job(self, request):
            sent["request"] = request
            return SimpleNamespace(
                metadata=SimpleNamespace(name="jobs/t/executions/x1")
            )

    class _Execs:
        def get_execution(self, name):
            return SimpleNamespace(completion_time=None)

    runner = CloudRunJobsRunner(
        "trend-trawler-bandit-traffic",
        project="p",
        region="us-central1",
        jobs_client_factory=_Jobs,
        executions_client_factory=_Execs,
    )
    name = asyncio.run(
        runner.run(
            experiment_id="e1",
            config_uri="gs://c",
            endpoint_id="endpoints/1",
            episodes=3,
            horizon=5000,
        )
    )
    assert name == "jobs/t/executions/x1"
    req = sent["request"]
    assert (
        req["name"]
        == "projects/p/locations/us-central1/jobs/trend-trawler-bandit-traffic"
    )
    env = {
        e["name"]: e["value"] for e in req["overrides"]["container_overrides"][0]["env"]
    }
    assert env == {
        "EXPERIMENT_ID": "e1",
        "CONFIG_URI": "gs://c",
        "ENDPOINT_ID": "endpoints/1",
        "EPISODES": "3",
        "HORIZON": "5000",
    }
    assert asyncio.run(runner.state(name)) == "running"
    assert asyncio.run(runner.state("not-an-execution")) == "unknown"


def test_run_request_is_a_valid_run_v2_message():
    from google.cloud import run_v2

    from runserver.experiments_jobs import build_run_job_request

    env = build_env_overrides(
        experiment_id="e", config_uri="c", endpoint_id="x", episodes=1, horizon=1000
    )
    msg = run_v2.RunJobRequest(
        build_run_job_request("projects/p/locations/r/jobs/j", env)
    )
    assert msg.overrides.container_overrides[0].env[4].value == "1000"


def test_fake_jobs_runner():
    jobs = FakeJobsRunner()
    name = asyncio.run(
        jobs.run(experiment_id="e", config_uri="c", endpoint_id="x", episodes=1)
    )
    assert asyncio.run(jobs.state(name)) == "running"
    jobs.finish(name)
    assert asyncio.run(jobs.state(name)) == "succeeded"
