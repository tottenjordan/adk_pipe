"""Endpoint lifecycle for bandit experiments behind a small ``Deployer`` protocol.

``deploy`` is stepwise and idempotent: it skips any resource already recorded in
``existing`` (``model_resource`` / ``endpoint_id`` / ``deployed_model_id``) and
reports each new id through ``on_step`` as soon as it exists, so the caller persists
it before the next (slow) step. A deploy interrupted by an api revision change then
resumes where it stopped instead of leaking a second model/endpoint.

``endpoint_id`` holds the full endpoint resource name
(``projects/P/locations/R/endpoints/N``).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

ID_FIELDS = ("model_resource", "endpoint_id", "deployed_model_id")
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

    async def teardown(self, row: dict) -> None:
        if not (row.get("endpoint_id") or row.get("model_resource")):
            return
        await asyncio.to_thread(
            self.lib.undeploy_and_delete,
            row.get("endpoint_id"),
            row.get("model_resource"),
        )

    async def state(self, row: dict) -> dict:
        if not row.get("endpoint_id"):
            return {"exists": False, "deployed": False}
        st = await asyncio.to_thread(self.lib.endpoint_state, row["endpoint_id"])
        return {"exists": st["exists"], "deployed": bool(st["deployed_models"])}


class FakeDeployer:
    """In-process stand-in (tests + ``BANDIT_DEPLOY_MODE=fake``): ready after ``delay``."""

    def __init__(self, delay: float = 0.0, fail_at: str | None = None):
        self.delay = delay
        self.fail_at = fail_at  # one of ID_FIELDS: raise instead of creating it
        self.created: dict[str, int] = {k: 0 for k in ID_FIELDS}
        self.endpoints: set[str] = set()
        self.models: set[str] = set()
        self.teardowns: list[str | None] = []
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
            await _report(on_step, {field: new_id})
        return ids

    async def teardown(self, row: dict) -> None:
        self.teardowns.append(row.get("endpoint_id"))
        self.endpoints.discard(row.get("endpoint_id") or "")
        self.models.discard(row.get("model_resource") or "")

    async def state(self, row: dict) -> dict:
        exists = bool(row.get("endpoint_id")) and row["endpoint_id"] in self.endpoints
        return {
            "exists": exists,
            "deployed": exists and bool(row.get("deployed_model_id")),
        }

    def drop(self, endpoint_id: str) -> None:
        """Simulate an endpoint deleted out from under the api."""
        self.endpoints.discard(endpoint_id)
