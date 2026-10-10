"""Shareable creative links REST API (owner side).

An owner shares one creative or a slate from a finished creative run as a public
link. Creating a share **freezes** it: the api builds the allowlisted snapshot
(``runserver/share_snapshot.py``), server-side copies the rendered images into
``gs://<bucket>/shares/<token>/<i>.png`` and writes
``shares/<token>/snapshot.json`` there, then records the share in
``creative_shares`` (``runserver/shares_store.py``). The separate public share
viewer reads only ``shares/<token>/``; revoking deletes those objects.

Creatives that cast a consented person (``generated_images[c].cast``) are shared
only when their ``consent_id`` is an active consent of the share owner with
``allow_public_share``; a slate leaves the others out (``skipped`` in the create
response) and naming one is a 400 ``person_not_shareable``. The included
consents are recorded on the row (``person_consent_ids``, never in the
snapshot), and revoking a consent revokes those shares
(``revoke_shares_for_consent``, a ``person_refs`` revoke hook).

Routes (all user-scoped by path, gated by ``UserAuthzMiddleware`` like
``/ratings/{user}/...``; a foreign or unknown session is a 404):

- ``POST /shares/{user}/{app}/{session}``: body ``{concept_names?: [str] (1-4),
  include_eval?: bool}`` → the share (``token``, ``url``, ``scope``, ...,
  ``skipped: [{concept_name, reason}]``).
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

from runserver import person_refs
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
from runserver.share_snapshot import (
    BuiltSnapshot,
    SnapshotError,
    build_snapshot,
    cast_consent_ids,
)
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
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RESERVED_FIRST = ("person-refs", "shares")

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


def output_prefix(state: Mapping[str, Any]) -> str | None:
    """The run's own output folder ``{gcs_folder}/{agent_output_dir}/``, where its
    renders are uploaded; None unless each part is one safe path segment (the
    same rules as the variants path), never ``variants`` or a reserved prefix."""
    folder, subdir = state.get("gcs_folder"), state.get("agent_output_dir")
    for segment in (folder, subdir):
        if (
            not isinstance(segment, str)
            or not _SEGMENT_RE.match(segment)
            or segment == "variants"
            or ".." in segment
        ):
            return None
    if folder in _RESERVED_FIRST:
        return None
    return f"{folder}/{subdir}/"


def source_path(uri: Any, bucket: str | None, prefix: str | None) -> str | None:
    """The object path of a source image, only for a file directly in the run's
    output folder ``gs://<bucket>/<prefix><file>`` (no ``..`` segments, never
    another share, a person photo or a variant); None otherwise.

    ``generated_images`` is session state, which a client can seed via
    createSession, so it must never make the api copy an arbitrary object (or
    another run's render) into a public share."""
    if not (isinstance(uri, str) and bucket and prefix):
        return None
    m = _GS_RE.match(uri)
    if not m or m["bucket"] != bucket:
        return None
    path = m["path"]
    if ".." in path.split("/") or path.startswith(SHARES_PREFIX):
        return None
    if is_person_image(uri):
        return None  # consented photos and personalised variants never go public
    name = path.removeprefix(prefix)
    if not path.startswith(prefix) or not name or "/" in name:
        return None
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
        if copied.metadata:
            # Never carry consent ids (or any custom metadata) into shares/: a
            # key patched to None is removed.
            copied.metadata = dict.fromkeys(copied.metadata)
        copied.patch()
    blob = bucket.blob(f"{prefix}{SNAPSHOT_NAME}")
    blob.cache_control = SNAPSHOT_CACHE_CONTROL
    blob.upload_from_string(data, content_type="application/json")


def _source_consents(bucket_name: str, paths: list[str]) -> list[str | None]:
    """Each source object's blob metadata ``consent_id`` (None when unset or the
    object is missing; blocking). Cast renders carry it from upload, so this
    trusts the object, not the client-seedable ``generated_images[c].cast``."""
    bucket = _gcs().bucket(bucket_name)
    out: list[str | None] = []
    for path in paths:
        blob = bucket.get_blob(path)
        cid = (blob.metadata or {}).get("consent_id") if blob is not None else None
        out.append(cid if isinstance(cid, str) and cid else None)
    return out


async def _consents_still_shareable(owner: str, consent_ids: list[str]) -> bool:
    for cid in consent_ids:
        record = await person_refs.active_consent(owner, cid)
        if record is None or record.get("allow_public_share") is not True:
            return False
    return True


def _delete_share_objects(bucket_name: str, token: str) -> None:
    """Delete every object under ``shares/<token>/`` (blocking)."""
    bucket = _gcs().bucket(bucket_name)
    for blob in bucket.list_blobs(prefix=f"{SHARES_PREFIX}{token}/"):
        blob.delete()


async def _resolve_consents(owner: str, state: Mapping[str, Any]) -> dict[str, dict]:
    """The run's cast consents that are active and the owner's (``consent_id`` →
    record). Raises the store error (the caller fails closed with a 503)."""
    found: dict[str, dict] = {}
    for cid in cast_consent_ids(state):
        record = await person_refs.active_consent(owner, cid)
        if record is not None:
            found[cid] = record
    return found


async def _revoke(user_id: str, token: str) -> bool:
    """Revoke the owner's share and delete ``shares/<token>/`` (idempotent).
    False when the token isn't the owner's; raises on a store or GCS failure."""
    if not await _STORE.revoke(token, user_id):
        return False
    bucket = _BUCKET
    if bucket:
        await asyncio.to_thread(_delete_share_objects, bucket, token)
    return True


async def revoke_shares_for_consent(record: Mapping[str, Any]) -> None:
    """``person_refs`` revoke hook: revoke every share of the consent's owner whose
    ``person_consent_ids`` contain it (revoked ones too, so a retry finishes
    deleting objects). Raises on failure so the consent revoke reports
    ``revoke_incomplete`` and can be repeated."""
    owner, cid = record.get("owner_user"), record.get("consent_id")
    if not (isinstance(owner, str) and isinstance(cid, str) and owner and cid):
        return
    rows = await _STORE.list_with_consent(owner, cid)
    for row in rows:
        await _revoke(owner, row["token"])
    if rows:
        log.info("shares: revoked %d share(s) for a revoked consent", len(rows))


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
    try:
        consents = await _resolve_consents(user_id, state)
    except Exception as exc:
        log.exception("shares: consent lookup failed")
        raise _error(
            503, "consent_unavailable", "could not check consents; retry shortly"
        ) from exc
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
            consent_lookup=consents.get,
        )
    except SnapshotError as exc:
        raise _error(400, exc.reason, str(exc)) from exc
    if any(is_person_image(uri) for uri in built.image_uris):
        raise _error(
            400,
            "person_image",
            "a creative's image is a person photo or personalised preview",
        )
    prefix = output_prefix(state)
    maybe = [source_path(uri, bucket, prefix) for uri in built.image_uris]
    sources = [p for p in maybe if p is not None]
    if len(sources) != len(maybe):
        raise _error(
            400,
            "image_outside_bucket",
            "a creative's image is not in this run's output folder",
        )
    try:
        object_consents = await asyncio.to_thread(_source_consents, bucket, sources)
    except Exception as exc:
        log.exception("shares: reading source image metadata failed")
        raise _error(502, "share_failed", "could not create the share") from exc
    if any(
        c is not None and c not in built.person_consent_ids for c in object_consents
    ):
        # A render made with a consented photo whose state says otherwise (or whose
        # consent doesn't cover public links) never goes public.
        raise _error(
            400,
            "person_not_shareable",
            "a creative shows a person whose consent doesn't cover public links",
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
        "person_consent_ids": built.person_consent_ids,
    }
    data = json.dumps(snapshot, ensure_ascii=False)
    try:
        await asyncio.to_thread(_publish, bucket, token, sources, data)
        await _STORE.put(row)
    except Exception as exc:
        log.exception("shares: creating share %s failed; rolling back", token)
        await _rollback(bucket, token)
        raise _error(502, "share_failed", "could not create the share") from exc
    if built.person_consent_ids:
        # A consent revoked (or narrowed) while the share was being created: its
        # revoke cascade may have run before the row existed, so undo it here.
        try:
            still_ok = await _consents_still_shareable(
                user_id, built.person_consent_ids
            )
        except Exception:
            log.exception("shares: consent re-check failed for share %s", token)
            still_ok = False
        if not still_ok:
            try:
                await _revoke(user_id, token)
            except Exception:
                log.exception("shares: revoking share %s failed", token)
                await _rollback(bucket, token)
            raise _error(
                409,
                "person_consent_changed",
                "a person's consent changed while the link was created; try again",
            )
    return {**to_public(row, _SHARE_BASE_URL), "skipped": built.skipped}


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
