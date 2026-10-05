"""deployment/bandit/endpoint.py against a fake aiplatform module (no GCP)."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest
from google.api_core import exceptions as gexc

from deployment.bandit import endpoint as ep


class _FakeEndpoint:
    instances: dict[str, _FakeEndpoint] = {}

    def __init__(self, endpoint_name: str = "", **_):
        if endpoint_name not in self.instances:
            raise gexc.NotFound("endpoint missing")
        src = self.instances[endpoint_name]
        self.__dict__ = src.__dict__  # share state with the created instance

    @classmethod
    def create(cls, display_name, labels):
        self = object.__new__(cls)
        self.display_name, self.labels = display_name, labels
        self.resource_name = (
            f"projects/p/locations/us-central1/endpoints/{len(cls.instances) + 1}"
        )
        self.deployed = []
        self.calls = []
        cls.instances[self.resource_name] = self
        return self

    def list_models(self):
        return list(self.deployed)

    def undeploy_all(self):
        self.calls.append("undeploy_all")
        self.deployed = []

    def delete(self):
        self.calls.append("delete")
        del self.instances[self.resource_name]


class _FakeModel:
    uploads: list[dict] = []
    deleted: list[str] = []
    existing: set[str] = set()

    def __init__(self, model_name: str = "", **_):
        if model_name not in self.existing:
            raise gexc.NotFound("model missing")
        self.resource_name = model_name

    @classmethod
    def upload(cls, **kwargs):
        cls.uploads.append(kwargs)
        m = object.__new__(cls)
        m.resource_name = f"projects/p/locations/us-central1/models/{len(cls.uploads)}"
        m.display_name = kwargs["display_name"]
        m.deploy_kwargs = None
        cls.existing.add(m.resource_name)
        return m

    def deploy(self, **kwargs):
        self.deploy_kwargs = kwargs
        kwargs["endpoint"].deployed.append(
            SimpleNamespace(id="dm-1", model=self.resource_name, display_name="x")
        )

    def delete(self):
        self.deleted.append(self.resource_name)
        self.existing.discard(self.resource_name)


@pytest.fixture
def fake_sdk(monkeypatch):
    _FakeEndpoint.instances = {}
    _FakeModel.uploads, _FakeModel.deleted, _FakeModel.existing = [], [], set()
    inits: list[dict] = []
    mod = ModuleType("google.cloud.aiplatform")
    mod.Endpoint = _FakeEndpoint  # type: ignore[attr-defined]
    mod.Model = _FakeModel  # type: ignore[attr-defined]
    mod.init = lambda **kw: inits.append(kw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.cloud.aiplatform", mod)
    import google.cloud

    monkeypatch.setattr(google.cloud, "aiplatform", mod, raising=False)
    monkeypatch.setenv("GCP_REGION", "europe-west4")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-x")
    return SimpleNamespace(inits=inits)


def test_upload_model_pins_single_worker_env_and_labels(fake_sdk):
    labels = ep.experiment_labels("Exp_1")
    model = ep.upload_model("img:1", "gs://b/bandit/Exp_1", "name", labels)
    (kw,) = _FakeModel.uploads
    assert kw == {
        "display_name": "name",
        "serving_container_image_uri": "img:1",
        "artifact_uri": "gs://b/bandit/Exp_1",
        "serving_container_environment_variables": {"VERTEX_CPR_WEB_CONCURRENCY": "1"},
        "labels": {"app": "trend-trawler", "experiment": "exp_1"},
    }
    assert model.resource_name.endswith("/models/1")
    assert fake_sdk.inits == [{"project": "proj-x", "location": "europe-west4"}]


def test_create_endpoint_and_deploy_one_replica(fake_sdk):
    model = ep.upload_model("img", "gs://a", "m", {})
    endpoint = ep.create_endpoint("e", {"app": "trend-trawler", "experiment": "x"})
    assert endpoint.labels == {"app": "trend-trawler", "experiment": "x"}
    dm_id = ep.deploy_model(model, endpoint, service_account="sa@p.iam")
    assert dm_id == "dm-1"
    kw = model.deploy_kwargs
    assert kw["endpoint"] is endpoint
    assert kw["min_replica_count"] == 1 and kw["max_replica_count"] == 1
    assert kw["machine_type"] == "n2-standard-2"
    assert kw["service_account"] == "sa@p.iam"
    assert kw["traffic_percentage"] == 100


def test_endpoint_state_and_undeploy_and_delete(fake_sdk):
    out = ep.create_all("exp1", "gs://a", "img")
    assert out["deployed_model_id"] == "dm-1"
    state = ep.endpoint_state(out["endpoint_id"])
    assert state["exists"] is True
    assert state["deployed_models"][0]["id"] == "dm-1"
    endpoint = _FakeEndpoint.instances[out["endpoint_id"]]
    ep.undeploy_and_delete(out["endpoint_id"])
    assert endpoint.calls == ["undeploy_all", "delete"]
    assert _FakeModel.deleted == [out["model_resource"]]
    assert ep.endpoint_state(out["endpoint_id"]) == {
        "exists": False,
        "deployed_models": [],
    }
    # Idempotent: already gone is fine.
    ep.undeploy_and_delete(out["endpoint_id"], out["model_resource"])


def test_undeploy_deletes_orphan_model_without_endpoint(fake_sdk):
    model = ep.upload_model("img", "gs://a", "m", {})
    ep.undeploy_and_delete(None, model.resource_name)
    assert _FakeModel.deleted == [model.resource_name]


def test_display_names_labels_and_default_region(monkeypatch):
    assert "abc123" in ep.display_name_for("abc123", "endpoint")
    assert ep.label_value("A.B@c") == "a-b-c"
    monkeypatch.delenv("GCP_REGION", raising=False)
    assert ep.region() == "us-central1"


def test_find_models_and_endpoints_by_label_oldest_first(fake_sdk):
    import datetime as dt

    t0 = dt.datetime(2026, 10, 5, 14, 3, tzinfo=dt.UTC)
    calls: list[tuple] = []

    def lister(items):
        def _list(**kwargs):
            calls.append(kwargs)
            return items

        return _list

    newer = SimpleNamespace(resource_name="m/2", create_time=t0 + dt.timedelta(61))
    older = SimpleNamespace(resource_name="m/1", create_time=t0)
    undated = SimpleNamespace(resource_name="m/3", create_time=None)
    _FakeModel.list = staticmethod(lister([newer, undated, older]))  # type: ignore[attr-defined]
    _FakeEndpoint.list = staticmethod(lister([older]))  # type: ignore[attr-defined]
    try:
        labels = ep.experiment_labels("2a7685cf9c8d4d3e")
        assert ep.find_models(labels) == ["m/1", "m/2", "m/3"]
        assert ep.find_endpoints(labels) == ["m/1"]
    finally:
        del _FakeModel.list, _FakeEndpoint.list  # type: ignore[attr-defined]
    want = 'labels.app="trend-trawler" AND labels.experiment="2a7685cf9c8d4d3e"'
    assert calls[0]["filter"] == want and calls[1]["filter"] == want
    assert fake_sdk.inits[0]["location"] == "europe-west4"
