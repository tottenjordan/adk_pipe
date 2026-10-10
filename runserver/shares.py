"""Shareable creative links REST API (owner side).

An owner shares one creative or a slate from a finished creative run as a public
link. Creating a share **freezes** it: the api builds the allowlisted snapshot
(``runserver/share_snapshot.py``), server-side copies the rendered images into
``gs://<bucket>/shares/<token>/<i>.png`` and writes
``shares/<token>/snapshot.json`` there, then records the share in
``creative_shares`` (``runserver/shares_store.py``). The separate public share
viewer reads only ``shares/<token>/``; revoking deletes those objects.

Routes (all user-scoped by path, gated by ``UserAuthzMiddleware`` like
``/ratings/{user}/...``; a foreign or unknown session is a 404):

- ``POST /shares/{user}/{app}/{session}``: body ``{concept_names?: [str] (1-4),
  include_eval?: bool}`` → the share (``token``, ``url``, ``scope``, ...).
- ``GET /shares/{user}``: the owner's active shares, newest first.
- ``DELETE /shares/{user}/{token}``: revoke (204; 404 when not the owner's).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from runserver.person_refs import is_person_image
from runserver.ratings import (
    RATING_APPS,
    REPORT_KEY,
    REPORT_URI_KEY,
    _as_obj,
    allowed_report_uri,
    configured_report_bucket,
    gcs_report_loader,
)
from runserver.share_snapshot import BuiltSnapshot, SnapshotError, build_snapshot
from runserver.shares_store import InMemorySharesStore, utcnow

log = logging.getLogger(__name__)

SHARE_APPS = RATING_APPS
MAX_CONCEPTS = 4
CONCEPT_NAME_MAX_CHARS = 512
MAX_ACTIVE_SHARES = 200
TITLE_MAX_CHARS = 200
TOKEN_BYTES = 16
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SHARES_PREFIX = "shares/"
IMAGE_CACHE_CONTROL = "private, max-age=300"
SNAPSHOT_CACHE_CONTROL = "no-store"
SNAPSHOT_NAME = "snapshot.json"
_APP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GS_RE = re.compile(r"^gs://(?P<bucket>[^/]+)/(?P<path>.+)$")

ReportLoader = Callable[[str], Any]


# --- Pure helpers -----------------------------------------------------------------


class ShareError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def validate_share_body(body: Mapping[str, Any]) -> tuple[list[str] | None, bool]:
    """``(concept_names, include_eval)`` (pure); raises ``ShareError``."""
    names = body.get("concept_names")
    if names is not None:
        if (
            not isinstance(names, list)
            or not 1 <= len(names) <= MAX_CONCEPTS
            or not all(
                isinstance(n, str) and 0 < len(n) <= CONCEPT_NAME_MAX_CHARS
                for n in names
            )
        ):
            raise ShareError(
                "invalid_concept_names",
                f"concept_names must be a list of 1-{MAX_CONCEPTS} concept names",
            )
        names = list(dict.fromkeys(names))
    include_eval = body.get("include_eval")
    if include_eval is None:
        include_eval = False
    if not isinstance(include_eval, bool):
        raise ShareError("invalid_include_eval", "include_eval must be a boolean")
    return names, include_eval


def source_path(uri: Any, bucket: str | None) -> str | None:
    """The object path of a source image, only under ``gs://<bucket>/`` (no
    ``..`` segments, never another share); None otherwise.

    ``generated_images`` is session state, which a client can seed via
    createSession, so it must never make the api copy an arbitrary object into a
    public share."""
    if not (isinstance(uri, str) and bucket):
        return None
    m = _GS_RE.match(uri)
    if not m or m["bucket"] != bucket:
        return None
    path = m["path"]
    if ".." in path.split("/") or path.startswith(SHARES_PREFIX):
        return None
    if is_person_image(uri):
        return None  # consented photos and personalised variants never go public
    return path


def share_url(token: str, base: str | None) -> str:
    """``<SHARE_BASE_URL>/s/<token>`` (relative ``/s/<token>`` without a base)."""
    return f"{(base or '').strip().rstrip('/')}/s/{token}"


def share_title(snapshot: Mapping[str, Any]) -> str:
    """ "<brand> × <trend>" for a slate, the headline for a single creative."""
    pair = " × ".join(t for t in (snapshot.get("brand"), snapshot.get("trend")) if t)
    title = pair or "Shared creatives"
    creatives = snapshot.get("creatives") or []
    if snapshot.get("scope") == "creative" and creatives:
        title = creatives[0].get("headline") or title
    return str(title)[:TITLE_MAX_CHARS]


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def to_public(row: Mapping[str, Any], base: str | None) -> dict:
    """A share as the API returns it (no ``owner_user`` / ``revoked_at``)."""
    return {
        "token": row["token"],
        "url": share_url(row["token"], base),
        "title": row.get("title") or "",
        "scope": row.get("scope"),
        "include_eval": bool(row.get("include_eval")),
        "concept_names": list(row.get("concept_names") or []),
        "app_name": row.get("app_name"),
        "session_id": row.get("session_id"),
        "created_at": _iso(row.get("created_at")),
    }


def _snapshot_time(now: dt.datetime) -> str:
    return now.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- Module state (configure) ---------------------------------------------------

_SESSION_SERVICE: Any = None
_STORE: Any = InMemorySharesStore()
_GCS_CLIENT: Any = None
_BUCKET: str | None = configured_report_bucket()
_SHARE_BASE_URL: str = os.environ.get("SHARE_BASE_URL", "")
_REPORT_LOADER: ReportLoader = gcs_report_loader


def configure(
    *,
    session_service,
    store,
    gcs_client: Any = None,
    bucket: str | None = None,
    share_base_url: str | None = None,
    report_loader: ReportLoader | None = None,
) -> None:
    """``gcs_client`` defaults to the shared lazy client, ``bucket`` to
    ``configured_report_bucket()`` (``GOOGLE_CLOUD_STORAGE_BUCKET``) and
    ``share_base_url`` to ``SHARE_BASE_URL``."""
    global _SESSION_SERVICE, _STORE, _GCS_CLIENT, _BUCKET, _SHARE_BASE_URL
    global _REPORT_LOADER
    _SESSION_SERVICE, _STORE, _GCS_CLIENT = session_service, store, gcs_client
    _BUCKET = (bucket or "").strip().removeprefix("gs://").strip("/") or (
        configured_report_bucket()
    )
    _SHARE_BASE_URL = (
        os.environ.get("SHARE_BASE_URL", "")
        if share_base_url is None
        else share_base_url
    )
    _REPORT_LOADER = report_loader or gcs_report_loader


def _gcs() -> Any:
    if _GCS_CLIENT is not None:
        return _GCS_CLIENT
    from agent_common.clients import get_gcs_client

    return get_gcs_client()


async def _load_report(state: Mapping[str, Any]) -> Mapping | None:
    """The run's GCS eval report (only under the configured bucket with the report
    filename), else the state copy, else None (fail soft: no eval block)."""
    uri = state.get(REPORT_URI_KEY)
    if allowed_report_uri(uri, _BUCKET):
        try:
            report = await asyncio.to_thread(_REPORT_LOADER, str(uri))
        except Exception:
            log.warning("shares: could not load eval report %s", uri, exc_info=True)
            report = None
        if isinstance(report, Mapping):
            return report
    elif uri:
        log.warning("shares: ignoring eval_report_gcs_uri outside the bucket")
    report = _as_obj(state.get(REPORT_KEY))
    return report if isinstance(report, Mapping) else None


def _publish(bucket_name: str, token: str, sources: list[str], data: str) -> None:
    """Copy the images and write the snapshot under ``shares/<token>/`` (blocking)."""
    bucket = _gcs().bucket(bucket_name)
    prefix = f"{SHARES_PREFIX}{token}/"
    for i, path in enumerate(sources):
        copied = bucket.copy_blob(bucket.blob(path), bucket, f"{prefix}{i}.png")
        copied.cache_control = IMAGE_CACHE_CONTROL
        copied.patch()
    blob = bucket.blob(f"{prefix}{SNAPSHOT_NAME}")
    blob.cache_control = SNAPSHOT_CACHE_CONTROL
    blob.upload_from_string(data, content_type="application/json")


def _delete_share_objects(bucket_name: str, token: str) -> None:
    """Delete every object under ``shares/<token>/`` (blocking)."""
    bucket = _gcs().bucket(bucket_name)
    for blob in bucket.list_blobs(prefix=f"{SHARES_PREFIX}{token}/"):
        blob.delete()


async def _rollback(bucket_name: str, token: str) -> None:
    try:
        await asyncio.to_thread(_delete_share_objects, bucket_name, token)
    except Exception:
        log.exception("shares: rollback of shares/%s/ failed", token)


# --- Routes -----------------------------------------------------------------------

router = APIRouter()


def _error(code: int, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=code, detail={"reason": reason, "message": message}
    )


class _ShareBody(BaseModel):
    # Typed loosely on purpose: validate_share_body turns bad values into a 400
    # with a reason (like /ratings), not a bare 422.
    concept_names: Any = None
    include_eval: Any = False


async def _get_session(app_name: str, user_id: str, session_id: str):
    from google.adk.errors.session_not_found_error import SessionNotFoundError

    try:
        return await _SESSION_SERVICE.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
    except SessionNotFoundError:
        return None


async def _active_shares(user_id: str) -> list[dict]:
    try:
        return await _STORE.list_for(user_id)
    except Exception as exc:
        log.exception("shares: store read failed")
        raise _error(502, "store_failed", "could not read shares") from exc


@router.post("/shares/{user_id}/{app_name}/{session_id}")
async def http_create_share(
    user_id: str, app_name: str, session_id: str, body: _ShareBody
) -> dict:
    if not _APP_RE.match(app_name) or app_name not in SHARE_APPS:
        raise _error(
            400, "invalid_app_name", f"app_name must be one of {list(SHARE_APPS)}"
        )
    try:
        concept_names, include_eval = validate_share_body(body.model_dump())
    except ShareError as exc:
        raise _error(400, exc.reason, str(exc)) from exc
    bucket = _BUCKET
    if not bucket:
        raise _error(503, "shares_unconfigured", "no share bucket is configured")
    # A foreign session raises VertexAiSessionService's ownership ValueError, which
    # install_ownership_handler maps to 404 too.
    session = await _get_session(app_name, user_id, session_id)
    if session is None:
        raise _error(404, "session_not_found", "session not found")
    if len(await _active_shares(user_id)) >= MAX_ACTIVE_SHARES:
        raise _error(
            429,
            "too_many_shares",
            f"at most {MAX_ACTIVE_SHARES} active shares; revoke one first",
        )
    state = dict(session.state or {})
    report = await _load_report(state) if include_eval else None
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = utcnow()
    try:
        built: BuiltSnapshot = build_snapshot(
            state,
            report,
            token=token,
            concept_names=concept_names,
            include_eval=include_eval,
            now=_snapshot_time(now),
        )
    except SnapshotError as exc:
        raise _error(400, exc.reason, str(exc)) from exc
    if any(is_person_image(uri) for uri in built.image_uris):
        raise _error(
            400,
            "person_image",
            "a creative's image is a person photo or personalised preview",
        )
    sources = [source_path(uri, bucket) for uri in built.image_uris]
    if any(p is None for p in sources):
        raise _error(
            400,
            "image_outside_bucket",
            "a creative's image is not in the configured bucket",
        )
    snapshot = built.snapshot
    row = {
        "token": token,
        "owner_user": user_id,
        "app_name": app_name,
        "session_id": session_id,
        "scope": snapshot["scope"],
        "concept_names": built.concept_names,
        "include_eval": include_eval,
        "title": share_title(snapshot),
        "created_at": now,
        "revoked_at": None,
    }
    data = json.dumps(snapshot, ensure_ascii=False)
    try:
        await asyncio.to_thread(
            _publish, bucket, token, [p for p in sources if p is not None], data
        )
        await _STORE.put(row)
    except Exception as exc:
        log.exception("shares: creating share %s failed; rolling back", token)
        await _rollback(bucket, token)
        raise _error(502, "share_failed", "could not create the share") from exc
    return to_public(row, _SHARE_BASE_URL)


@router.get("/shares/{user_id}")
async def http_list_shares(user_id: str) -> dict:
    rows = await _active_shares(user_id)
    return {"shares": [to_public(r, _SHARE_BASE_URL) for r in rows]}


@router.delete("/shares/{user_id}/{token}", status_code=204)
async def http_revoke_share(user_id: str, token: str) -> Response:
    if not TOKEN_RE.match(token):
        raise _error(404, "share_not_found", "share not found")
    try:
        revoked = await _STORE.revoke(token, user_id)
    except Exception as exc:
        log.exception("shares: revoke %s failed", token)
        raise _error(502, "store_failed", "could not revoke the share") from exc
    if not revoked:
        raise _error(404, "share_not_found", "share not found")
    bucket = _BUCKET
    if bucket:
        try:
            await asyncio.to_thread(_delete_share_objects, bucket, token)
        except Exception as exc:
            # The row is revoked (hidden from the list); retrying the idempotent
            # DELETE finishes removing the objects.
            log.exception("shares: deleting shares/%s/ failed", token)
            raise _error(
                502, "revoke_incomplete", "share revoked but not fully deleted; retry"
            ) from exc
    return Response(status_code=204)
