"""Endpoint lifecycle for bandit experiments behind a small ``Deployer`` protocol.

``deploy`` is stepwise and idempotent: it skips any resource already recorded in
``existing`` (``model_resource`` / ``endpoint_id`` / ``deployed_model_id``) and
reports each new id through ``on_step`` as soon as it exists, so the caller persists
it before the next (slow) step. A deploy interrupted by an api revision change then
resumes where it stopped instead of leaking a second model/endpoint.

Ids recorded on the row come first; when one is missing, ``deploy`` looks for a
resource labelled ``app=trend-trawler,experiment=<id>`` before creating it (the
oldest wins, extras are logged and removed by ``teardown``), so even a deploy whose
progress write was lost doesn't upload a duplicate. The api's deploy lease
(``runserver.experiments_store``) keeps two deployers from racing in the first place.

``endpoint_id`` holds the full endpoint resource name
(``projects/P/locations/R/endpoints/N``).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

ID_FIELDS = ("model_resource", "endpoint_id", "deployed_model_id")
# The label-discoverable resources (a deployed model lives on its endpoint).
LABELLED_FIELDS = ("model_resource", "endpoint_id")
StepCallback = Callable[[dict], Awaitable[None]]


class Deployer(Protocol):
    async def deploy(
        self,
        experiment_id: str,
        artifact_uri: str,
        *,
        existing: dict | None = None,
        on_step: StepCallback | None = None,
    ) -> dict:
        """Create model + endpoint + deployment; returns the ``ID_FIELDS`` dict."""
        ...

    async def teardown(self, row: dict) -> None:
        """Undeploy + delete whatever the row records; a no-op when already gone."""
        ...

    async def state(self, row: dict) -> dict:
        """``{"exists": bool, "deployed": bool}`` for the row's endpoint."""
        ...

    async def find_existing(self, experiment_id: str) -> dict:
        """``{"model_resource", "endpoint_id"}`` of the oldest resources labelled
        for ``experiment_id`` (None when there is none)."""
        ...


def _oldest(names: list[str], kind: str, experiment_id: str) -> str | None:
    if len(names) > 1:
        log.warning(
            "bandit %s: %d %ss labelled for it; reusing the oldest %s, extras %s",
            experiment_id,
            len(names),
            kind,
            names[0],
            names[1:],
        )
    return names[0] if names else None


async def _report(on_step: StepCallback | None, ids: dict) -> None:
    if on_step is not None:
        await on_step(ids)


class VertexDeployer:
    """Wraps ``deployment/bandit/endpoint.py``; every SDK call runs in a thread."""

    def __init__(
        self,
        image_uri: str,
        *,
        machine_type: str = "n2-standard-2",
        service_account: str | None = None,
        lib: Any = None,
    ):
        self.image_uri = image_uri
        self.machine_type = machine_type
        self.service_account = service_account
        self._lib = lib

    @property
    def lib(self) -> Any:
        if self._lib is None:
            from deployment.bandit import endpoint

            self._lib = endpoint
        return self._lib

    async def deploy(
        self,
        experiment_id: str,
        artifact_uri: str,
        *,
        existing: dict | None = None,
        on_step: StepCallback | None = None,
    ) -> dict:
        if not self.image_uri:
            raise RuntimeError("BANDIT_SERVING_IMAGE is not set")
        lib = self.lib
        ids = {k: (existing or {}).get(k) for k in ID_FIELDS}
        labels = lib.experiment_labels(experiment_id)
        if not ids["model_resource"]:
            found = await asyncio.to_thread(lib.find_models, labels)
            ids["model_resource"] = _oldest(found, "model", experiment_id)
            if not ids["model_resource"]:
                model = await asyncio.to_thread(
                    lib.upload_model,
                    self.image_uri,
                    artifact_uri,
                    lib.display_name_for(experiment_id, "model"),
                    labels,
                )
                ids["model_resource"] = model.resource_name
            await _report(on_step, {"model_resource": ids["model_resource"]})
        if not ids["endpoint_id"]:
            found = await asyncio.to_thread(lib.find_endpoints, labels)
            ids["endpoint_id"] = _oldest(found, "endpoint", experiment_id)
            if not ids["endpoint_id"]:
                endpoint = await asyncio.to_thread(
                    lib.create_endpoint,
                    lib.display_name_for(experiment_id, "endpoint"),
                    labels,
                )
                ids["endpoint_id"] = endpoint.resource_name
            await _report(on_step, {"endpoint_id": ids["endpoint_id"]})
        if not ids["deployed_model_id"]:
            # A deploy LRO may have finished after the previous process died: adopt it.
            current = await asyncio.to_thread(lib.endpoint_state, ids["endpoint_id"])
            if current["deployed_models"]:
                ids["deployed_model_id"] = current["deployed_models"][0]["id"]
            else:
                model = await asyncio.to_thread(lib.get_model, ids["model_resource"])
                endpoint = await asyncio.to_thread(lib.get_endpoint, ids["endpoint_id"])
                ids["deployed_model_id"] = await asyncio.to_thread(
                    lib.deploy_model,
                    model,
                    endpoint,
                    self.machine_type,
                    self.service_account,
                )
            await _report(on_step, {"deployed_model_id": ids["deployed_model_id"]})
        return ids

    async def _labelled(self, experiment_id: str) -> tuple[list[str], list[str]]:
        labels = self.lib.experiment_labels(experiment_id)
        models = await asyncio.to_thread(self.lib.find_models, labels)
        endpoints = await asyncio.to_thread(self.lib.find_endpoints, labels)
        return models, endpoints

    async def teardown(self, row: dict) -> None:
        """Delete the recorded endpoint/model, then any other resources labelled
        for the experiment (duplicates from a raced or unrecorded deploy)."""
        endpoint_id, model = row.get("endpoint_id"), row.get("model_resource")
        if endpoint_id or model:
            await asyncio.to_thread(self.lib.undeploy_and_delete, endpoint_id, model)
        if not row.get("experiment_id"):
            return
        try:
            models, endpoints = await self._labelled(row["experiment_id"])
        except Exception:
            log.exception("bandit %s: label lookup for teardown", row["experiment_id"])
            return
        for extra in (e for e in endpoints if e != endpoint_id):
            await asyncio.to_thread(self.lib.undeploy_and_delete, extra, None)
        for extra in (m for m in models if m != model):
            await asyncio.to_thread(self.lib.undeploy_and_delete, None, extra)

    async def state(self, row: dict) -> dict:
        if not row.get("endpoint_id"):
            return {"exists": False, "deployed": False}
        st = await asyncio.to_thread(self.lib.endpoint_state, row["endpoint_id"])
        return {"exists": st["exists"], "deployed": bool(st["deployed_models"])}

    async def find_existing(self, experiment_id: str) -> dict:
        models, endpoints = await self._labelled(experiment_id)
        return {
            "model_resource": _oldest(models, "model", experiment_id),
            "endpoint_id": _oldest(endpoints, "endpoint", experiment_id),
        }


class FakeDeployer:
    """In-process stand-in (tests + ``BANDIT_DEPLOY_MODE=fake``): ready after ``delay``."""

    def __init__(self, delay: float = 0.0, fail_at: str | None = None):
        self.delay = delay
        self.fail_at = fail_at  # one of ID_FIELDS: raise instead of creating it
        self.created: dict[str, int] = {k: 0 for k in ID_FIELDS}
        self.endpoints: set[str] = set()
        self.models: set[str] = set()
        self.teardowns: list[str | None] = []
        # Label index: experiment_id -> field -> resource names, oldest first.
        self.labelled: dict[str, dict[str, list[str]]] = {}
        self.deployed_on: dict[str, str] = {}  # endpoint -> deployed model id
        self._seq = itertools.count(1)
        self.gate: asyncio.Event | None = None  # tests: block deploy until set

    async def deploy(
        self,
        experiment_id: str,
        artifact_uri: str,
        *,
        existing: dict | None = None,
        on_step: StepCallback | None = None,
    ) -> dict:
        ids = {k: (existing or {}).get(k) for k in ID_FIELDS}
        found = await self.find_existing(experiment_id)
        for field in LABELLED_FIELDS:
            ids[field] = ids[field] or found[field]
        if ids["endpoint_id"] and not ids["deployed_model_id"]:
            ids["deployed_model_id"] = self.deployed_on.get(ids["endpoint_id"])
        base = "projects/fake/locations/us-central1"
        for field, make in (
            ("model_resource", lambda n: f"{base}/models/{n}"),
            ("endpoint_id", lambda n: f"{base}/endpoints/{n}"),
            ("deployed_model_id", lambda n: f"dm-{n}"),
        ):
            if ids[field]:
                continue
            if self.gate is not None and field == "deployed_model_id":
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_at == field:
                raise RuntimeError(f"fake deploy failure at {field}")
            new_id = make(next(self._seq))
            ids[field] = new_id
            self.created[field] += 1
            if field == "model_resource":
                self.models.add(new_id)
            elif field == "endpoint_id":
                self.endpoints.add(new_id)
            else:
                self.deployed_on[str(ids["endpoint_id"])] = new_id
            if field in LABELLED_FIELDS:
                index = self.labelled.setdefault(experiment_id, {})
                index.setdefault(field, []).append(new_id)
            await _report(on_step, {field: new_id})
        return ids

    async def teardown(self, row: dict) -> None:
        self.teardowns.append(row.get("endpoint_id"))
        doomed = {row.get("endpoint_id") or "", row.get("model_resource") or ""}
        for names in self.labelled.get(row.get("experiment_id") or "", {}).values():
            doomed.update(names)  # labelled extras go too (VertexDeployer parity)
        self.endpoints -= doomed
        self.models -= doomed
        for endpoint in doomed & set(self.deployed_on):
            del self.deployed_on[endpoint]

    async def find_existing(self, experiment_id: str) -> dict:
        index = self.labelled.get(experiment_id, {})
        live = {"model_resource": self.models, "endpoint_id": self.endpoints}
        return {
            field: next((n for n in index.get(field, []) if n in live[field]), None)
            for field in LABELLED_FIELDS
        }

    async def state(self, row: dict) -> dict:
        exists = bool(row.get("endpoint_id")) and row["endpoint_id"] in self.endpoints
        return {
            "exists": exists,
            "deployed": exists and bool(row.get("deployed_model_id")),
        }

    def drop(self, endpoint_id: str) -> None:
        """Simulate an endpoint deleted out from under the api."""
        self.endpoints.discard(endpoint_id)
