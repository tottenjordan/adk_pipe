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
    srv.server_close()
    thread.join(timeout=5)


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
    def __init__(self, fail_times=0, error=ServiceUnavailable):
        self.fail_times = fail_times
        self.error = error
        self.timeouts = []
        self.init_kwargs = None
        self.endpoint_kwargs = None
        self.predict_calls = []

    def init(self, **kw):
        self.init_kwargs = kw

    def Endpoint(self, **kw):  # noqa: N802
        self.endpoint_kwargs = kw
        sdk = self

        class _Endpoint:
            def predict(self, instances, parameters=None, timeout=None):
                sdk.predict_calls.append((instances, parameters))
                sdk.timeouts.append(timeout)
                if sdk.fail_times:
                    sdk.fail_times -= 1
                    raise sdk.error("503 unavailable")
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


class DeadlineExceeded(Exception):
    """Named like google.api_core's: what a predict past its timeout raises."""


def test_vertex_client_bounds_each_predict_so_a_stall_is_retried():
    # the SDK's default timeout is None (wait forever); a bounded call turns a
    # stalled predict into a DeadlineExceeded that call_with_retries retries
    sdk = FakeAiplatform(fail_times=1, error=DeadlineExceeded)
    client = ec.VertexEndpointClient(
        ENDPOINT, aiplatform_module=sdk, sleep=lambda s: None
    )
    insts = [{"type": "state"}]
    assert client.predict(insts) == _echo(insts)
    assert sdk.timeouts == [ec.PREDICT_TIMEOUT_SECONDS] * 2
    # capped at 20 s, so a stalled call returns close to the SIGTERM grace window
    assert ec.PREDICT_TIMEOUT_SECONDS == 20


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


# ------------------------------------------------ stop requests (C6, SIGTERM)


def test_call_with_retries_checks_stop_before_a_retry_sleep():
    # SIGTERM lands while the first attempt is stalled: no backoff sleep, no
    # second attempt, StopRequested instead
    stop = {"set": False}
    sleeps, calls = [], []

    def fn():
        calls.append(1)
        stop["set"] = True
        raise ec.TransientEndpointError("504")

    with pytest.raises(ec.StopRequested):
        ec.call_with_retries(fn, sleep=sleeps.append, should_stop=lambda: stop["set"])
    assert calls == [1] and sleeps == []


def test_call_with_retries_checks_stop_before_every_attempt():
    calls = []
    with pytest.raises(ec.StopRequested):
        ec.call_with_retries(lambda: calls.append(1), should_stop=lambda: True)
    assert calls == []


def test_call_with_retries_checks_stop_after_a_sleep():
    # the flag set during the backoff sleep stops before the next attempt
    stop = {"set": False}
    calls = []

    def fn():
        calls.append(1)
        raise ec.TransientEndpointError("503")

    def sleep(_s):
        stop["set"] = True

    with pytest.raises(ec.StopRequested):
        ec.call_with_retries(fn, sleep=sleep, should_stop=lambda: stop["set"])
    assert calls == [1]


def test_call_with_retries_retryable_predicate():
    sleeps = []
    calls = iter([ValueError("conflict"), "ok"])

    def fn():
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v

    out = ec.call_with_retries(
        fn, sleep=sleeps.append, retryable=lambda exc: True, what="progress"
    )
    assert out == "ok" and len(sleeps) == 1


def test_vertex_client_stops_a_stalled_predict_instead_of_retrying():
    stop = {"set": False}

    class Stalled(FakeAiplatform):
        def Endpoint(self, **kw):  # noqa: N802
            inner = super().Endpoint(**kw)

            class _E:
                def predict(self, **kw):
                    stop["set"] = True  # SIGTERM arrives during the stall
                    return inner.predict(**kw)

            return _E()

    sdk = Stalled(fail_times=10, error=DeadlineExceeded)
    sleeps = []
    client = ec.VertexEndpointClient(
        ENDPOINT,
        aiplatform_module=sdk,
        sleep=sleeps.append,
        should_stop=lambda: stop["set"],
    )
    with pytest.raises(ec.StopRequested):
        client.predict([{"type": "state"}])
    assert len(sdk.predict_calls) == 1 and sleeps == []


def test_http_and_in_process_clients_check_stop():
    session = SimpleNamespace(post=lambda *a, **k: pytest.fail("posted"))
    with pytest.raises(ec.StopRequested):
        ec.HttpClient("http://x", session=session, should_stop=lambda: True).predict(
            [{"type": "state"}]
        )

    class Target:
        def predict(self, instances, parameters):
            pytest.fail("called")

    with pytest.raises(ec.StopRequested):
        ec.InProcessClient(Target(), should_stop=lambda: True).predict(
            [{"type": "state"}]
        )
