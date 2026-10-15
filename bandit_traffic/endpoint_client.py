"""Clients for the bandit CPR endpoint (contracts §2).

Every client exposes ``predict(instances, parameters=None) -> list[dict]``: a list
of §2 instances in, one prediction per instance out, in order. Per-instance
failures come back as ``{"type": "error", ...}`` predictions (never raised);
only request-level failures raise.

- ``VertexEndpointClient``: a deployed Vertex AI endpoint, addressed by its full
  resource name ``projects/P/locations/R/endpoints/N`` (contracts §6). The
  ``google.cloud.aiplatform`` import is lazy (injectable for tests).
- ``HttpClient``: a local CPR container (``POST {instances, parameters}`` to its
  predict URL, parse ``predictions``).
- ``InProcessClient``: wraps any object with a ``predict(instances, parameters)``
  method (the fake endpoint now, ``bandit_serving``'s ``BanditPredictor`` later).

The network clients retry transient failures (429 / 5xx / connection errors) with
exponential backoff. Retrying a request is safe: decisions are recomputed and
rewards are deduplicated by ``request_id`` on the server.

Every client takes an optional ``should_stop`` callable (the job's SIGTERM
flag). ``call_with_retries`` checks it before every attempt and before every
backoff sleep and raises ``StopRequested`` once it is true, so a stalled
endpoint can't hold the job past Cloud Run's SIGKILL grace window; the traffic
loop then flushes its buffered events and stops (exit 143).

No JAX here: this module is importable anywhere.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any, Protocol

log = logging.getLogger(__name__)

TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
#: Per-call bound on ``Endpoint.predict`` (the SDK default ``None`` waits
#: forever): a stalled predict raises ``DeadlineExceeded``, which
#: ``call_with_retries`` retries instead of hanging until the job's timeout.
#: 20 s, because a call in flight can't be interrupted cheaply: after SIGTERM the
#: job waits at most one such call before it sees the stop flag (a 500-instance
#: request normally takes well under a second).
PREDICT_TIMEOUT_SECONDS = 20.0
_TRANSIENT_NAMES = frozenset(
    {
        "ServiceUnavailable",
        "TooManyRequests",
        "ResourceExhausted",
        "InternalServerError",
        "BadGateway",
        "GatewayTimeout",
        "DeadlineExceeded",
        "ConnectionError",
        "Timeout",
        "ConnectTimeout",
        "ReadTimeout",
        "TransportError",
    }
)
# Vertex online prediction limits a request to 1.5 MB; stay well under it.
MAX_REQUEST_BYTES = 1_200_000
MAX_REQUEST_INSTANCES = 500
_RESOURCE_RE = re.compile(
    r"^projects/(?P<project>[^/]+)/locations/(?P<location>[^/]+)/endpoints/(?P<endpoint>[^/]+)$"
)


class EndpointClient(Protocol):
    def predict(
        self, instances: Sequence[dict], parameters: dict | None = None
    ) -> list[dict]: ...


class EndpointError(RuntimeError):
    """A request-level failure (after retries), or a malformed response."""


class TransientEndpointError(EndpointError):
    """A retryable request-level failure (e.g. HTTP 429/5xx)."""


class StopRequested(RuntimeError):
    """``should_stop`` returned true before an attempt or a backoff sleep."""


def _check_stop(should_stop: Callable[[], bool] | None, what: str) -> None:
    if should_stop is not None and should_stop():
        raise StopRequested(f"stop requested before {what}")


def is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` is worth retrying (429 / 5xx / timeouts / connection)."""
    if isinstance(exc, TransientEndpointError):
        return True
    for attr in ("code", "status_code"):
        code = getattr(exc, attr, None)
        if isinstance(code, int) and code in TRANSIENT_STATUS:
            return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status in TRANSIENT_STATUS:
        return True
    return any(cls.__name__ in _TRANSIENT_NAMES for cls in type(exc).__mro__)


def call_with_retries(
    fn: Callable[[], Any],
    *,
    attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    what: str = "predict",
    should_stop: Callable[[], bool] | None = None,
    retryable: Callable[[BaseException], bool] = is_transient,
) -> Any:
    """Call ``fn``, retrying failures ``retryable`` accepts (default: transient
    ones) with jittered exponential backoff.

    ``should_stop`` is checked before every attempt and before every backoff
    sleep; once it returns true, ``StopRequested`` is raised instead."""
    for attempt in range(1, attempts + 1):
        _check_stop(should_stop, what)
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts or not retryable(exc):
                raise
            # SIGTERM during a stalled call: stop now instead of sleeping and
            # retrying (the caller flushes its buffered rows)
            _check_stop(should_stop, f"retrying {what}")
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay *= 0.5 + random.random() / 2
            log.warning(
                "%s attempt %d/%d failed (%s: %s); retrying in %.2fs",
                what,
                attempt,
                attempts,
                type(exc).__name__,
                exc,
                delay,
            )
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def split_instances(
    instances: Sequence[dict],
    *,
    max_instances: int = MAX_REQUEST_INSTANCES,
    max_bytes: int = MAX_REQUEST_BYTES,
) -> Iterator[list[dict]]:
    """Chunk ``instances`` (in order) so each request body stays under both limits.

    Sizes are measured on the compact JSON encoding plus a small envelope
    allowance. A single instance larger than ``max_bytes`` is sent alone."""
    envelope = 64
    chunk: list[dict] = []
    size = envelope
    for inst in instances:
        n = len(json.dumps(inst, separators=(",", ":"))) + 1
        if chunk and (len(chunk) >= max_instances or size + n > max_bytes):
            yield chunk
            chunk, size = [], envelope
        chunk.append(inst)
        size += n
    if chunk:
        yield chunk


def _check_predictions(predictions: Any, n: int) -> list[dict]:
    if not isinstance(predictions, list) or len(predictions) != n:
        got = len(predictions) if isinstance(predictions, list) else type(predictions)
        raise EndpointError(f"expected {n} predictions, got {got}")
    return [dict(p) for p in predictions]


def parse_resource_name(name: str) -> dict[str, str]:
    """``projects/P/locations/R/endpoints/N`` -> ``{project, location, endpoint}``."""
    m = _RESOURCE_RE.match(name.strip())
    if not m:
        raise ValueError(
            f"ENDPOINT_ID must be a full resource name "
            f"projects/P/locations/R/endpoints/N, got {name!r}"
        )
    return m.groupdict()


class InProcessClient:
    """Calls ``target.predict(instances, parameters)`` directly (no network)."""

    def __init__(self, target: Any, *, should_stop: Callable[[], bool] | None = None):
        self.target = target
        self.should_stop = should_stop

    def predict(
        self, instances: Sequence[dict], parameters: dict | None = None
    ) -> list[dict]:
        _check_stop(self.should_stop, "predict")
        out = self.target.predict(list(instances), parameters)
        if isinstance(out, dict):  # a predictor returning the HTTP envelope
            out = out.get("predictions")
        return _check_predictions(out, len(instances))


class HttpClient:
    """POSTs ``{instances, parameters}`` to a local CPR predict URL."""

    def __init__(
        self,
        url: str,
        *,
        session: Any = None,
        timeout: float = PREDICT_TIMEOUT_SECONDS,
        attempts: int = 5,
        base_delay: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] | None = None,
    ):
        self.url = url
        self.should_stop = should_stop
        self.timeout = timeout
        self.attempts = attempts
        self.base_delay = base_delay
        self.sleep = sleep
        self._session = session

    def _http(self) -> Any:
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _post(self, body: dict) -> list[dict]:
        resp = self._http().post(self.url, json=body, timeout=self.timeout)
        status = int(resp.status_code)
        if status in TRANSIENT_STATUS:
            raise TransientEndpointError(f"HTTP {status}: {resp.text[:200]}")
        if status >= 400:
            raise EndpointError(f"HTTP {status}: {resp.text[:200]}")
        payload = resp.json()
        if not isinstance(payload, dict) or "predictions" not in payload:
            raise EndpointError("response has no 'predictions'")
        return payload["predictions"]

    def predict(
        self, instances: Sequence[dict], parameters: dict | None = None
    ) -> list[dict]:
        body: dict[str, Any] = {"instances": list(instances)}
        if parameters:
            body["parameters"] = parameters
        preds = call_with_retries(
            lambda: self._post(body),
            attempts=self.attempts,
            base_delay=self.base_delay,
            sleep=self.sleep,
            what=f"POST {self.url}",
            should_stop=self.should_stop,
        )
        return _check_predictions(preds, len(instances))


class VertexEndpointClient:
    """``aiplatform.Endpoint(endpoint_name=...).predict`` with retry and backoff.

    The region comes from the resource name (an endpoint is regional, never the
    ``global`` model location). ``aiplatform_module`` injects a fake SDK in tests."""

    def __init__(
        self,
        endpoint_name: str,
        *,
        aiplatform_module: Any = None,
        attempts: int = 6,
        base_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] | None = None,
    ):
        parts = parse_resource_name(endpoint_name)
        self.should_stop = should_stop
        self.endpoint_name = endpoint_name.strip()
        self.project = parts["project"]
        self.location = parts["location"]
        self.attempts = attempts
        self.base_delay = base_delay
        self.sleep = sleep
        self._sdk = aiplatform_module
        self._endpoint: Any = None

    def _get_endpoint(self) -> Any:
        if self._endpoint is None:
            sdk = self._sdk
            if sdk is None:
                from google.cloud import aiplatform as sdk

            sdk.init(project=self.project, location=self.location)
            self._endpoint = sdk.Endpoint(
                endpoint_name=self.endpoint_name,
                project=self.project,
                location=self.location,
            )
        return self._endpoint

    def predict(
        self, instances: Sequence[dict], parameters: dict | None = None
    ) -> list[dict]:
        def call() -> Any:
            return self._get_endpoint().predict(
                instances=list(instances),
                parameters=parameters or None,
                timeout=PREDICT_TIMEOUT_SECONDS,
            )

        resp = call_with_retries(
            call,
            attempts=self.attempts,
            base_delay=self.base_delay,
            sleep=self.sleep,
            what=f"predict {self.endpoint_name}",
            should_stop=self.should_stop,
        )
        return _check_predictions(
            list(getattr(resp, "predictions", [])), len(instances)
        )
