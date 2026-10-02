"""``bandit_traffic.endpoint_client``: request splitting, retries, and the HTTP /
Vertex / in-process clients (stub server and fake SDK; no GCP)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from bandit_traffic import endpoint_client as ec

ENDPOINT = "projects/p1/locations/us-central1/endpoints/123"


def _echo(instances):
    return [
        {"type": i.get("type"), "request_id": i.get("request_id")} for i in instances
    ]


def test_split_instances_respects_both_limits():
    insts = [
        {"type": "decision", "request_id": f"r{i}", "pad": "x" * 90} for i in range(50)
    ]
    parts = list(ec.split_instances(insts, max_instances=8, max_bytes=10**9))
    assert [len(p) for p in parts] == [8] * 6 + [2]
    parts = list(ec.split_instances(insts, max_instances=500, max_bytes=1000))
    assert sum(parts, []) == insts  # order preserved
    for p in parts:
        assert len(json.dumps({"instances": p}, separators=(",", ":"))) <= 1000
    big = [{"pad": "x" * 5000}]
    assert list(ec.split_instances(big, max_bytes=1000)) == [big]


def test_is_transient():
    assert ec.is_transient(ec.TransientEndpointError("503"))
    assert ec.is_transient(type("E", (Exception,), {"code": 429})())
    ServiceUnavailable = type("ServiceUnavailable", (Exception,), {})
    assert ec.is_transient(ServiceUnavailable("x"))
    assert not ec.is_transient(ValueError("bad"))
    assert not ec.is_transient(type("E", (Exception,), {"code": 400})())


def test_call_with_retries_backs_off_then_succeeds():
    sleeps = []
    calls = iter(
        [ec.TransientEndpointError("503"), ec.TransientEndpointError("429"), "ok"]
    )

    def fn():
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v

    assert ec.call_with_retries(fn, base_delay=1.0, sleep=sleeps.append) == "ok"
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] * 0.9


def test_call_with_retries_does_not_retry_permanent_errors():
    sleeps = []

    def fn():
        raise ec.EndpointError("HTTP 400")

    with pytest.raises(ec.EndpointError):
        ec.call_with_retries(fn, sleep=sleeps.append)
    assert sleeps == []


class _Handler(BaseHTTPRequestHandler):
    statuses: list[int] = []
    bodies: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).bodies.append(body)
        status = type(self).statuses.pop(0) if type(self).statuses else 200
        payload = (
            {"predictions": _echo(body["instances"])}
            if status == 200
            else {"error": "x"}
        )
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Handler.statuses, _Handler.bodies = [], []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/predict"
    srv.shutdown()


def test_http_client_round_trip_and_retry_on_503(server):
    _Handler.statuses = [503]
    sleeps = []
    client = ec.HttpClient(server, sleep=sleeps.append)
    insts = [{"type": "decision", "request_id": "a"}, {"type": "state"}]
    preds = client.predict(insts, {"exploration_scale": 1.0})
    assert preds == _echo(insts)
    assert len(sleeps) == 1
    assert _Handler.bodies[-1] == {
        "instances": insts,
        "parameters": {"exploration_scale": 1.0},
    }


def test_http_client_permanent_error_raises(server):
    _Handler.statuses = [400]
    with pytest.raises(ec.EndpointError, match="HTTP 400"):
        ec.HttpClient(server, sleep=lambda s: None).predict([{"type": "state"}])


def test_http_client_with_fake_session_checks_prediction_count():
    resp = SimpleNamespace(status_code=200, json=lambda: {"predictions": []}, text="")
    session = SimpleNamespace(post=lambda url, json, timeout: resp)
    with pytest.raises(ec.EndpointError, match="expected 1 predictions"):
        ec.HttpClient("http://x", session=session).predict([{"type": "state"}])


class ServiceUnavailable(Exception):
    code = 503


class FakeAiplatform:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.init_kwargs = None
        self.endpoint_kwargs = None
        self.predict_calls = []

    def init(self, **kw):
        self.init_kwargs = kw

    def Endpoint(self, **kw):  # noqa: N802
        self.endpoint_kwargs = kw
        sdk = self

        class _Endpoint:
            def predict(self, instances, parameters=None):
                sdk.predict_calls.append((instances, parameters))
                if sdk.fail_times:
                    sdk.fail_times -= 1
                    raise ServiceUnavailable("503 unavailable")
                return SimpleNamespace(predictions=_echo(instances))

        return _Endpoint()


def test_vertex_client_uses_resource_name_region_and_retries():
    sdk = FakeAiplatform(fail_times=2)
    sleeps = []
    client = ec.VertexEndpointClient(
        ENDPOINT, aiplatform_module=sdk, sleep=sleeps.append
    )
    insts = [{"type": "reset", "episode": 0, "seed": 1}]
    assert client.predict(insts) == _echo(insts)
    assert sdk.init_kwargs == {"project": "p1", "location": "us-central1"}
    assert sdk.endpoint_kwargs == {
        "endpoint_name": ENDPOINT,
        "project": "p1",
        "location": "us-central1",
    }
    assert len(sdk.predict_calls) == 3 and len(sleeps) == 2
    assert sdk.predict_calls[0][1] is None


def test_vertex_client_gives_up_after_attempts():
    sdk = FakeAiplatform(fail_times=10)
    client = ec.VertexEndpointClient(
        ENDPOINT, aiplatform_module=sdk, attempts=3, sleep=lambda s: None
    )
    with pytest.raises(ServiceUnavailable):
        client.predict([{"type": "state"}])
    assert len(sdk.predict_calls) == 3


@pytest.mark.parametrize("name", ["123", "projects/p/endpoints/1", ""])
def test_vertex_client_requires_full_resource_name(name):
    with pytest.raises(ValueError, match="full resource name"):
        ec.VertexEndpointClient(name, aiplatform_module=FakeAiplatform())


def test_in_process_client():
    class Target:
        def predict(self, instances, parameters):
            return _echo(instances)

    insts = [{"type": "state"}]
    assert ec.InProcessClient(Target()).predict(insts) == _echo(insts)

    class Envelope:
        def predict(self, instances, parameters):
            return {"predictions": _echo(instances)}

    assert ec.InProcessClient(Envelope()).predict(insts) == _echo(insts)

    class Short:
        def predict(self, instances, parameters):
            return []

    with pytest.raises(ec.EndpointError):
        ec.InProcessClient(Short()).predict(insts)
