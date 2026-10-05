"""Vertex AI model upload + endpoint deploy/undeploy for bandit experiments.

Library (used by ``runserver/experiments_deploy.VertexDeployer``) plus a small absl
CLI for manual use::

    python -m deployment.bandit.endpoint --create --experiment_id=exp1 \
        --artifact_uri=gs://BUCKET/bandit/exp1 --image_uri=REGION-docker.pkg.dev/...
    python -m deployment.bandit.endpoint --state --endpoint=projects/.../endpoints/123
    python -m deployment.bandit.endpoint --delete --endpoint=projects/.../endpoints/123

Every call is blocking (the SDK polls long-running operations); async callers wrap
them in ``asyncio.to_thread``. The SDK import is lazy (``_sdk``) so importing this
module stays cheap and tests can swap in a fake module. An endpoint is a *regional*
resource, so the location is ``GCP_REGION`` (default ``us-central1``), never the
``global`` model location.

The CPR container is always uploaded with ``VERTEX_CPR_WEB_CONCURRENCY=1``: one web
worker holds the single in-memory posterior (contracts §2); more workers would
each learn from a disjoint slice of the rewards.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

APP_LABEL = "trend-trawler"
SERVING_ENV = {"VERTEX_CPR_WEB_CONCURRENCY": "1"}
DEFAULT_MACHINE_TYPE = "n2-standard-2"


def region() -> str:
    return os.getenv("GCP_REGION", "us-central1")


def project() -> str | None:
    return os.getenv("GOOGLE_CLOUD_PROJECT")


def _sdk() -> Any:
    """The initialised ``google.cloud.aiplatform`` module (lazy import)."""
    from google.cloud import aiplatform

    aiplatform.init(project=project(), location=region())
    return aiplatform


def label_value(value: str) -> str:
    """Coerce ``value`` into a valid GCP label value (lowercase ``[a-z0-9_-]``, <=63)."""
    return re.sub(r"[^a-z0-9_-]", "-", value.lower())[:63]


def experiment_labels(experiment_id: str) -> dict[str, str]:
    return {"app": APP_LABEL, "experiment": label_value(experiment_id)}


def display_name_for(experiment_id: str, kind: str) -> str:
    """Model/endpoint display name; always carries the experiment id."""
    return f"trend-trawler-bandit-{kind}-{experiment_id}"


def upload_model(
    image_uri: str, artifact_uri: str, display_name: str, labels: dict[str, str]
) -> Any:
    """Upload the CPR serving image as a Model whose artifacts are ``artifact_uri``
    (``AIP_STORAGE_URI`` -> ``experiment.json`` + ``checkpoints/``)."""
    return _sdk().Model.upload(
        display_name=display_name,
        serving_container_image_uri=image_uri,
        artifact_uri=artifact_uri,
        serving_container_environment_variables=dict(SERVING_ENV),
        labels=dict(labels),
    )


def create_endpoint(display_name: str, labels: dict[str, str]) -> Any:
    return _sdk().Endpoint.create(display_name=display_name, labels=dict(labels))


def label_filter(labels: dict[str, str]) -> str:
    """Vertex list ``filter`` matching every label (``labels.k="v" AND ...``)."""
    return " AND ".join(f'labels.{k}="{v}"' for k, v in sorted(labels.items()))


def _oldest_first(resources: Any) -> list[str]:
    """Resource names sorted by ``create_time`` (undated last, then by name)."""

    def key(r: Any) -> tuple[float, str]:
        created = getattr(r, "create_time", None)
        return (created.timestamp() if created else float("inf"), r.resource_name)

    return [r.resource_name for r in sorted(resources, key=key)]


def find_models(labels: dict[str, str]) -> list[str]:
    """Models carrying every label in ``labels`` (in ``GCP_REGION``), oldest first.

    Lets a resumed deploy adopt a model an earlier attempt uploaded but never
    recorded, instead of uploading a duplicate."""
    return _oldest_first(
        _sdk().Model.list(filter=label_filter(labels), order_by="create_time")
    )


def find_endpoints(labels: dict[str, str]) -> list[str]:
    """Endpoints carrying every label in ``labels``, oldest first."""
    return _oldest_first(
        _sdk().Endpoint.list(filter=label_filter(labels), order_by="create_time")
    )


def get_model(model_resource: str) -> Any:
    return _sdk().Model(model_name=model_resource)


def get_endpoint(endpoint_resource: str) -> Any:
    return _sdk().Endpoint(endpoint_name=endpoint_resource)


def deploy_model(
    model: Any,
    endpoint: Any,
    machine_type: str = DEFAULT_MACHINE_TYPE,
    service_account: str | None = None,
) -> str:
    """Deploy ``model`` onto ``endpoint`` with exactly one replica (one posterior);
    returns the deployed model id."""
    model.deploy(
        endpoint=endpoint,
        deployed_model_display_name=getattr(model, "display_name", None),
        machine_type=machine_type,
        min_replica_count=1,
        max_replica_count=1,
        traffic_percentage=100,
        service_account=service_account,
    )
    deployed = endpoint.list_models()
    return str(deployed[-1].id) if deployed else ""


def _is_not_found(exc: Exception) -> bool:
    from google.api_core import exceptions as gexc

    return isinstance(exc, gexc.NotFound)


def undeploy_and_delete(
    endpoint_resource: str | None, model_resource: str | None = None
) -> None:
    """Undeploy every model from the endpoint, delete it, then delete the model(s).

    Idempotent: a missing endpoint/model is skipped. Models deployed on the endpoint
    are deleted too, so ``model_resource`` only matters when the deploy step never
    ran (model uploaded, nothing deployed yet)."""
    sdk = _sdk()
    models: set[str] = {model_resource} if model_resource else set()
    if endpoint_resource:
        try:
            endpoint = sdk.Endpoint(endpoint_name=endpoint_resource)
            models.update(
                str(dm.model)
                for dm in endpoint.list_models()
                if getattr(dm, "model", "")
            )
            endpoint.undeploy_all()
            endpoint.delete()
        except Exception as exc:
            if not _is_not_found(exc):
                raise
            log.info("endpoint %s already gone", endpoint_resource)
    for name in sorted(models):
        try:
            sdk.Model(model_name=name).delete()
        except Exception as exc:
            if not _is_not_found(exc):
                raise
            log.info("model %s already gone", name)


def endpoint_state(endpoint_resource: str) -> dict:
    """``{"exists": bool, "deployed_models": [{"id", "model", "display_name"}]}``."""
    try:
        endpoint = _sdk().Endpoint(endpoint_name=endpoint_resource)
        deployed = endpoint.list_models()
    except Exception as exc:
        if _is_not_found(exc):
            return {"exists": False, "deployed_models": []}
        raise
    return {
        "exists": True,
        "deployed_models": [
            {
                "id": str(dm.id),
                "model": str(getattr(dm, "model", "") or ""),
                "display_name": str(getattr(dm, "display_name", "") or ""),
            }
            for dm in deployed
        ],
    }


def create_all(
    experiment_id: str,
    artifact_uri: str,
    image_uri: str,
    machine_type: str = DEFAULT_MACHINE_TYPE,
    service_account: str | None = None,
) -> dict:
    """Upload + create endpoint + deploy in one go (CLI convenience)."""
    labels = experiment_labels(experiment_id)
    model = upload_model(
        image_uri, artifact_uri, display_name_for(experiment_id, "model"), labels
    )
    endpoint = create_endpoint(display_name_for(experiment_id, "endpoint"), labels)
    deployed_id = deploy_model(model, endpoint, machine_type, service_account)
    return {
        "model_resource": model.resource_name,
        "endpoint_id": endpoint.resource_name,
        "deployed_model_id": deployed_id,
    }


def _define_flags() -> None:  # pragma: no cover - manual CLI
    from absl import flags

    flags.DEFINE_bool("create", False, "upload model, create endpoint, deploy")
    flags.DEFINE_bool("delete", False, "undeploy all, delete endpoint and models")
    flags.DEFINE_bool("state", False, "print endpoint state")
    flags.DEFINE_string("experiment_id", None, "experiment id (labels, names)")
    flags.DEFINE_string("artifact_uri", None, "gs:// dir holding experiment.json")
    flags.DEFINE_string(
        "image_uri", os.getenv("BANDIT_SERVING_IMAGE"), "CPR serving image"
    )
    flags.DEFINE_string("machine_type", DEFAULT_MACHINE_TYPE, "machine type")
    flags.DEFINE_string(
        "service_account", os.getenv("BANDIT_ENDPOINT_SA", ""), "endpoint SA email"
    )
    flags.DEFINE_string("endpoint", None, "endpoint resource name")
    flags.DEFINE_string("model", None, "model resource name (delete only)")
    flags.mark_bool_flags_as_mutual_exclusive(["create", "delete", "state"])


def main(argv: list[str]) -> None:  # pragma: no cover - manual CLI
    import json

    from absl import flags

    del argv
    f = flags.FLAGS
    if f.create:
        if not (f.experiment_id and f.artifact_uri and f.image_uri):
            raise SystemExit(
                "--create needs --experiment_id --artifact_uri --image_uri"
            )
        result = create_all(
            f.experiment_id,
            f.artifact_uri,
            f.image_uri,
            f.machine_type,
            f.service_account or None,
        )
        print(json.dumps(result, indent=2))
    elif f.delete:
        if not f.endpoint:
            raise SystemExit("--delete needs --endpoint")
        undeploy_and_delete(f.endpoint, f.model or None)
        print(f"deleted {f.endpoint}")
    elif f.state:
        if not f.endpoint:
            raise SystemExit("--state needs --endpoint")
        print(json.dumps(endpoint_state(f.endpoint), indent=2))
    else:
        raise SystemExit("pass one of --create / --delete / --state")


if __name__ == "__main__":  # pragma: no cover - manual CLI
    from absl import app

    _define_flags()
    app.run(main)
