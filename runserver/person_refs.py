"""Person-reference consent registry REST API (owner side).

A user registers a consented photo of a person (an adult who agreed to appear in
AI-generated ad previews) so later runs can cast them. The photo is uploaded out of
band to ``gs://<bucket>/person-refs/<owner slug>/<file>.(jpg|jpeg|png|webp)``
(``slug_for``: ``<readable>-<h>``, the lower-case email with ``@`` and ``.``
replaced by ``_`` plus 10 hex chars of its sha256); this
api only records the consent in ``person_references``
(``runserver/person_refs_store.py``) after checking the photo sits under the
caller's own prefix and is a readable image.

Revoking cascades, in order: mark the record revoked; run the registered
``revoke_hooks`` with the re-read record (``deployment/async_app.py`` registers
``shares.revoke_shares_for_consent``, which revokes every share whose
``person_consent_ids`` contain it); delete every image made with the photo, listed
in the record's ``person_renders`` (cast base renders, recorded by
``record_renders`` from the api run path, and personalised variants, recorded at
upload), plus the cast renders and variants found in the state of every run
recorded on it as a ``session:<app>/<session_id>`` marker (``record_session``, at
kick-off, so a run that died mid-segment is covered too), but only objects still
carrying blob metadata ``consent_id`` = this consent (session state is
client-seedable, so a recorded URI alone never deletes an object); then delete
the photo. Every step is idempotent: a failure is a 502
``revoke_incomplete`` and repeating the DELETE finishes the cleanup.

Routes (user-scoped by path, gated by ``UserAuthzMiddleware`` like ``/shares``):

- ``POST /person-refs/{user}``: body ``{photo_uri, label, subject,
  adult_attested, allow_public_share, consent_text_version}`` → the record.
- ``GET /person-refs/{user}``: ``{person_refs: [...], prefix,
  consent_text_version}`` (active records, newest first; ``prefix`` is the
  caller's upload prefix ``gs://<bucket>/person-refs/<slug>/``).
- ``DELETE /person-refs/{user}/{consent_id}``: revoke (204; 404 when not the
  owner's).

Person photo URIs are never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from runserver.person_refs_store import InMemoryPersonRefsStore, utcnow
from runserver.ratings import configured_report_bucket

log = logging.getLogger(__name__)

# The consent wording the owner agrees to, mirrored verbatim in
# frontend/src/lib/person-consent.ts (drift-tested). Change both together and bump
# the version to the change's date: a POST with an older version is refused.
CONSENT_TEXT_VERSION = "2026-10-09"
CONSENT_TEXT = (
    "By registering this photo you confirm that the person shown is an adult "
    "(18 or over) and has agreed to appear in AI-generated ad previews made with "
    "Trend Trawler. The photo is used only as a reference for generating images in "
    "your runs and is visible only to you. Images made with it appear in public "
    "share links only if the person also agreed to that. You can revoke at any "
    "time: revoking deletes the photo and every image made with it."
)

PERSON_REFS_PREFIX = "person-refs/"
SUBJECTS = ("self", "third_party_with_consent")
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_ACTIVE_REFS = 50
LABEL_MAX_CHARS = 80
CONSENT_ID_BYTES = 12
CONSENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}\.(?:jpg|jpeg|png|webp)$")
_GS_RE = re.compile(r"^gs://(?P<bucket>[^/]+)/(?P<path>.+)$")
# Blob metadata key carried by every image made with a consented photo (cast base
# renders: creative_agent/image_tools.py; variants: runserver/variants.py).
RENDER_METADATA_KEY = "consent_id"
# Never deleted as a "render": public share copies (revoked by their own cascade)
# and the consented photos themselves.
_NOT_RENDER_PREFIXES = ("shares/", PERSON_REFS_PREFIX)
# ``person_renders`` entries that name a run (``session:<app>/<session_id>``)
# rather than an object; written at kick-off by the /runs path.
SESSION_MARKER_PREFIX = "session:"
_APP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

RevokeHook = Callable[[dict], Awaitable[None] | None]


# --- Pure helpers -----------------------------------------------------------------


class PersonRefError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


SLUG_HASH_CHARS = 10


def slug_for(user: str) -> str:
    """The owner's ``person-refs/`` folder name: ``<readable>-<h>``.

    ``readable`` is the normalized (stripped, lower-case) email with ``@`` and ``.``
    replaced by ``_``; ``h`` is the first 10 hex chars of the sha256 of the
    normalized email. The readable part alone isn't injective (``a.b@x.com`` and
    ``a_b@x.com`` both give ``a_b_x_com``); the hash keeps owners apart. Mirrored by
    ``emailSlug`` in frontend/src/lib/person-paths.ts (shared golden fixture
    tests/fixtures/person_slugs.json)."""
    email = user.strip().lower()
    readable = email.replace("@", "_").replace(".", "_")
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:SLUG_HASH_CHARS]
    return f"{readable}-{digest}"


def owner_prefix(user: str) -> str:
    """``person-refs/<slug>/`` (the object-path prefix of the owner's photos)."""
    return f"{PERSON_REFS_PREFIX}{slug_for(user)}/"


# Folder segments of person-derived objects: consented photos (``person-refs/``)
# and personalised variant previews (``…/variants/…``, runserver/variants.py).
PERSON_IMAGE_SEGMENTS = ("person-refs", "variants")


def is_person_image(uri: Any) -> bool:
    """True for a ``gs://`` URI (or object path) with a ``person-refs`` or ``variants``
    folder segment. Such images never enter experiments or public shares."""
    if not isinstance(uri, str) or not uri:
        return False
    path = uri.removeprefix("gs://")
    folders = path.split("/")[1:-1] if uri.startswith("gs://") else path.split("/")[:-1]
    return any(seg in PERSON_IMAGE_SEGMENTS for seg in folders)


def photo_path(uri: Any, user: str, bucket: str | None) -> str | None:
    """The object path of a person photo, only for
    ``gs://<bucket>/person-refs/<slug(user)>/<file>.(jpg|jpeg|png|webp)`` (one
    level, no ``..``); None otherwise."""
    if not (isinstance(uri, str) and bucket):
        return None
    m = _GS_RE.match(uri)
    if not m or m["bucket"] != bucket:
        return None
    path = m["path"]
    prefix = owner_prefix(user)
    if not path.startswith(prefix):
        return None
    name = path[len(prefix) :]
    if not _FILE_RE.match(name.lower()) or ".." in name:
        return None
    return path


def render_path(uri: Any, bucket: str | None) -> str | None:
    """The object path of an image made with a consented photo, only for
    ``gs://<bucket>/<path>`` with no ``..`` segment, outside ``shares/`` and
    ``person-refs/``; None otherwise."""
    if not (isinstance(uri, str) and bucket):
        return None
    m = _GS_RE.match(uri)
    if not m or m["bucket"] != bucket:
        return None
    path = m["path"]
    if ".." in path.split("/") or path.startswith(_NOT_RENDER_PREFIXES):
        return None
    return path


def session_marker(app_name: Any, session_id: Any) -> str | None:
    """``session:<app>/<session_id>`` (None for a malformed app or session id)."""
    if not (
        isinstance(app_name, str)
        and isinstance(session_id, str)
        and _APP_RE.match(app_name)
        and _SESSION_ID_RE.match(session_id)
    ):
        return None
    return f"{SESSION_MARKER_PREFIX}{app_name}/{session_id}"


def parse_session_marker(entry: Any) -> tuple[str, str] | None:
    """``(app, session_id)`` of a ``session:`` entry, None otherwise."""
    if not isinstance(entry, str) or not entry.startswith(SESSION_MARKER_PREFIX):
        return None
    app_name, sep, session_id = entry[len(SESSION_MARKER_PREFIX) :].partition("/")
    if not sep or session_marker(app_name, session_id) != entry:
        return None
    return app_name, session_id


def session_render_uris(state: Mapping[str, Any] | None, consent_id: str) -> list[str]:
    """The ``gcs_uri`` of every ``generated_images`` entry and ``person_variants``
    record that names ``consent_id`` (pure; deletion still checks the object's
    metadata)."""
    state = state or {}
    uris: list[str] = []

    def take(record: Any) -> None:
        if isinstance(record, Mapping) and record.get("consent_id") == consent_id:
            uri = record.get("gcs_uri")
            if isinstance(uri, str) and uri:
                uris.append(uri)

    images = state.get("generated_images")
    if isinstance(images, Mapping):
        for record in images.values():
            take(record)
    variants = state.get("person_variants")
    if isinstance(variants, Mapping):
        for by_key in variants.values():
            if isinstance(by_key, Mapping):
                for record in by_key.values():
                    take(record)
    return list(dict.fromkeys(uris))


def validate_body(
    body: Mapping[str, Any], user: str, bucket: str
) -> tuple[str, str, str, bool]:
    """``(photo_path, label, subject, allow_public_share)`` (pure); raises
    ``PersonRefError``."""
    path = photo_path(body.get("photo_uri"), user, bucket)
    if path is None:
        raise PersonRefError(
            "invalid_photo_uri",
            f"photo_uri must be gs://{bucket}/{owner_prefix(user)}<file>"
            ".(jpg|jpeg|png|webp)",
        )
    label = body.get("label")
    label = label.strip() if isinstance(label, str) else ""
    if not 0 < len(label) <= LABEL_MAX_CHARS:
        raise PersonRefError(
            "invalid_label", f"label must be 1-{LABEL_MAX_CHARS} characters"
        )
    subject = body.get("subject")
    if subject not in SUBJECTS:
        raise PersonRefError("invalid_subject", f"subject must be one of {SUBJECTS}")
    if body.get("adult_attested") is not True:
        raise PersonRefError(
            "adult_attestation_required",
            "confirm the person is an adult who agreed to appear",
        )
    allow = body.get("allow_public_share")
    if allow is None:
        allow = False
    if not isinstance(allow, bool):
        raise PersonRefError(
            "invalid_allow_public_share", "allow_public_share must be a boolean"
        )
    if body.get("consent_text_version") != CONSENT_TEXT_VERSION:
        raise PersonRefError(
            "stale_consent_text",
            "the consent text changed; reload the page and agree to the current text",
        )
    return path, label, str(subject), allow


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def to_public(row: Mapping[str, Any]) -> dict:
    """A record as the API returns it (no ``owner_user`` / ``revoked_at`` /
    ``person_renders``)."""
    return {
        "consent_id": row["consent_id"],
        "photo_uri": row.get("photo_uri"),
        "label": row.get("label") or "",
        "subject": row.get("subject"),
        "adult_attested": bool(row.get("adult_attested")),
        "allow_public_share": bool(row.get("allow_public_share")),
        "consent_text_version": row.get("consent_text_version"),
        "created_at": _iso(row.get("created_at")),
    }


# --- Module state (configure) ---------------------------------------------------

_STORE: Any = InMemoryPersonRefsStore()
_GCS_CLIENT: Any = None
_BUCKET: str | None = configured_report_bucket()
# Called with the (re-read) revoked record after it's marked revoked, before the
# recorded renders and the photo are deleted (async_app registers the share
# cascade here). Each hook must be idempotent: a failed revoke is retried by
# repeating the DELETE.
_REVOKE_HOOKS: list[RevokeHook] = []
revoke_hooks = _REVOKE_HOOKS
# Reads the state of runs recorded on a consent (``session:`` markers) at revoke.
_SESSION_SERVICE: Any = None


def configure(
    *,
    store,
    gcs_client: Any = None,
    bucket: str | None = None,
    revoke_hooks: Iterable[RevokeHook] | None = None,
    session_service: Any = None,
) -> None:
    """``gcs_client`` defaults to the shared lazy client, ``bucket`` to
    ``configured_report_bucket()`` (``GOOGLE_CLOUD_STORAGE_BUCKET``);
    ``session_service`` reads recorded runs at revoke (None = skip that step)."""
    global _STORE, _GCS_CLIENT, _BUCKET, _SESSION_SERVICE
    _STORE, _GCS_CLIENT, _SESSION_SERVICE = store, gcs_client, session_service
    _BUCKET = (bucket or "").strip().removeprefix("gs://").strip("/") or (
        configured_report_bucket()
    )
    _REVOKE_HOOKS.clear()
    _REVOKE_HOOKS.extend(revoke_hooks or ())


def _gcs() -> Any:
    if _GCS_CLIENT is not None:
        return _GCS_CLIENT
    from agent_common.clients import get_gcs_client

    return get_gcs_client()


def _photo_readable(bucket_name: str, path: str) -> bool:
    """The object exists, is ≤ ``MAX_PHOTO_BYTES`` and is ``image/*`` (blocking)."""
    blob = _gcs().bucket(bucket_name).get_blob(path)
    if blob is None:
        return False
    size, ctype = blob.size, blob.content_type or ""
    return (
        isinstance(size, int)
        and 0 < size <= MAX_PHOTO_BYTES
        and (ctype.lower().startswith("image/"))
    )


def _delete_photo(bucket_name: str, path: str) -> None:
    """Delete the photo object; an already-deleted object is fine (blocking)."""
    from google.api_core.exceptions import NotFound

    try:
        _gcs().bucket(bucket_name).blob(path).delete()
    except NotFound:
        pass


def _delete_renders(bucket_name: str, consent_id: str, uris: Iterable[Any]) -> int:
    """Delete each recorded render that still carries this consent's metadata
    (blocking); returns the number deleted. Raises on a GCS failure."""
    from google.api_core.exceptions import NotFound

    bucket = _gcs().bucket(bucket_name)
    deleted = skipped = 0
    for uri in uris:
        path = render_path(uri, bucket_name)
        if path is None:
            continue
        blob = bucket.get_blob(path)
        if blob is None:
            continue
        if (blob.metadata or {}).get(RENDER_METADATA_KEY) != consent_id:
            skipped += 1  # overwritten since, or never made with this consent
            continue
        try:
            blob.delete()
            deleted += 1
        except NotFound:
            pass
    if skipped:
        log.warning(
            "person refs: kept %d recorded render(s) without this consent's metadata",
            skipped,
        )
    return deleted


async def delete_renders(record: Mapping[str, Any]) -> None:
    """Delete the images recorded in ``record["person_renders"]`` (see
    ``_delete_renders``); raises on failure (the revoke is retryable)."""
    bucket, cid = _BUCKET, record.get("consent_id")
    uris = list(record.get("person_renders") or [])
    if not (bucket and isinstance(cid, str) and uris):
        return
    deleted = await asyncio.to_thread(_delete_renders, bucket, cid, uris)
    if deleted:
        log.info(
            "person refs: deleted %d image(s) made with a revoked consent", deleted
        )


async def _session_state(owner: str, app_name: str, session_id: str) -> Any:
    from google.adk.errors.session_not_found_error import SessionNotFoundError

    try:
        session = await _SESSION_SERVICE.get_session(
            app_name=app_name, user_id=owner, session_id=session_id
        )
    except SessionNotFoundError:
        return None
    return session.state if session is not None else None


async def delete_session_renders(record: Mapping[str, Any]) -> None:
    """For every run recorded on the consent (``session:`` markers), delete the
    cast renders and variants its state lists for this consent (metadata-checked,
    see ``_delete_renders``). A deleted session is skipped; a read failure raises
    (the revoke is retryable)."""
    bucket, cid = _BUCKET, record.get("consent_id")
    owner = record.get("owner_user")
    if _SESSION_SERVICE is None or not (
        bucket and isinstance(cid, str) and isinstance(owner, str)
    ):
        return
    for entry in record.get("person_renders") or []:
        parsed = parse_session_marker(entry)
        if parsed is None:
            continue
        state = await _session_state(owner, *parsed)
        uris = session_render_uris(state, cid)
        if uris:
            await asyncio.to_thread(_delete_renders, bucket, cid, uris)


async def record_session(
    owner: str, consent_id: str, app_name: str, session_id: str
) -> bool:
    """Mark a run that uses ``owner``'s consent (a ``session:<app>/<id>`` entry in
    ``person_renders``, deduplicated) so a revoke can read its state even when
    the run never finished. False for a malformed id or another owner's consent;
    raises on a store error."""
    marker = session_marker(app_name, session_id)
    if marker is None or not (
        isinstance(consent_id, str) and CONSENT_ID_RE.match(consent_id)
    ):
        return False
    return bool(await _STORE.add_renders(consent_id, owner, [marker]))


async def record_renders(owner: str, consent_id: str, uris: Iterable[Any]) -> bool:
    """Record images made with ``owner``'s consent on its ``person_renders`` so a
    revoke deletes them (only run outputs in the bucket, see ``render_path``;
    deduplicated). Returns True while the consent is active and the owner's. When
    it was revoked meanwhile, the images are deleted at once and False is
    returned. Raises on a store error."""
    if not isinstance(consent_id, str) or not CONSENT_ID_RE.match(consent_id):
        return False
    bucket = _BUCKET
    keep = [u for u in dict.fromkeys(uris) if render_path(u, bucket)]
    if keep and not await _STORE.add_renders(consent_id, owner, keep):
        return False  # not the owner's consent
    if await _STORE.active_for(consent_id, owner) is not None:
        return True
    # Revoked while rendering: the revoke may already have run, so delete now.
    if keep and bucket:
        await asyncio.to_thread(_delete_renders, bucket, consent_id, keep)
    return False


async def active_consent(owner: str, consent_id: str) -> dict | None:
    """The consent record when it exists, belongs to ``owner`` and isn't revoked
    (for the run and variant paths of later PRs); None otherwise."""
    if not isinstance(consent_id, str) or not CONSENT_ID_RE.match(consent_id):
        return None
    return await _STORE.active_for(consent_id, owner)


# --- Routes -----------------------------------------------------------------------

router = APIRouter()


def _error(code: int, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=code, detail={"reason": reason, "message": message}
    )


class _PersonRefBody(BaseModel):
    # Typed loosely on purpose: validate_body turns bad values into a 400 with a
    # reason (like /shares), not a bare 422.
    photo_uri: Any = None
    label: Any = None
    subject: Any = None
    adult_attested: Any = None
    allow_public_share: Any = False
    consent_text_version: Any = None


async def _active_refs(user_id: str) -> list[dict]:
    try:
        return await _STORE.list_for(user_id)
    except Exception as exc:
        log.exception("person refs: store read failed")
        raise _error(502, "store_failed", "could not read person references") from exc


@router.post("/person-refs/{user_id}")
async def http_create_person_ref(user_id: str, body: _PersonRefBody) -> dict:
    bucket = _BUCKET
    if not bucket:
        raise _error(
            503, "person_refs_unconfigured", "no person-reference bucket is configured"
        )
    try:
        path, label, subject, allow = validate_body(body.model_dump(), user_id, bucket)
    except PersonRefError as exc:
        raise _error(400, exc.reason, str(exc)) from exc
    photo_uri = f"gs://{bucket}/{path}"
    active = await _active_refs(user_id)
    if any(r.get("photo_uri") == photo_uri for r in active):
        # Revoking one record deletes the photo, which would break the other.
        raise _error(
            409, "already_registered", "this photo is already registered; revoke it"
        )
    if len(active) >= MAX_ACTIVE_REFS:
        raise _error(
            429,
            "too_many_person_refs",
            f"at most {MAX_ACTIVE_REFS} people; revoke one first",
        )
    try:
        readable = await asyncio.to_thread(_photo_readable, bucket, path)
    except Exception:
        log.warning("person refs: photo check failed", exc_info=True)
        readable = False
    if not readable:
        raise _error(
            400,
            "photo_unreadable",
            f"the photo must exist and be an image of at most "
            f"{MAX_PHOTO_BYTES // (1024 * 1024)} MB",
        )
    row = {
        "consent_id": secrets.token_urlsafe(CONSENT_ID_BYTES),
        "owner_user": user_id,
        "photo_uri": photo_uri,
        "label": label,
        "subject": subject,
        "adult_attested": True,
        "allow_public_share": allow,
        "consent_text_version": CONSENT_TEXT_VERSION,
        "created_at": utcnow(),
        "revoked_at": None,
        "person_renders": [],
    }
    try:
        await _STORE.put(row)
    except Exception as exc:
        log.exception("person refs: store write failed")
        raise _error(502, "store_failed", "could not save the consent") from exc
    return to_public(row)


@router.get("/person-refs/{user_id}")
async def http_list_person_refs(user_id: str) -> dict:
    rows = await _active_refs(user_id)
    bucket = _BUCKET
    return {
        "person_refs": [to_public(r) for r in rows],
        "prefix": f"gs://{bucket}/{owner_prefix(user_id)}" if bucket else None,
        "consent_text_version": CONSENT_TEXT_VERSION,
    }


@router.delete("/person-refs/{user_id}/{consent_id}", status_code=204)
async def http_revoke_person_ref(user_id: str, consent_id: str) -> Response:
    not_found = _error(404, "person_ref_not_found", "person reference not found")
    if not CONSENT_ID_RE.match(consent_id):
        raise not_found
    try:
        row = await _STORE.get(consent_id)
        if row is None or row.get("owner_user") != user_id:
            raise not_found
        if not await _STORE.revoke(consent_id, user_id):
            raise not_found
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("person refs: revoke %s failed", consent_id)
        raise _error(502, "store_failed", "could not revoke the consent") from exc
    try:
        # Re-read after the revoke: a render recorded meanwhile is either listed
        # here or deleted by record_renders itself (it re-checks after appending).
        row = await _STORE.get(consent_id) or row
        for hook in list(_REVOKE_HOOKS):
            result = hook(dict(row))
            if inspect.isawaitable(result):
                await result
        await delete_renders(row)
        await delete_session_renders(row)
        bucket = _BUCKET
        path = photo_path(row.get("photo_uri"), user_id, bucket)
        if bucket and path:
            await asyncio.to_thread(_delete_photo, bucket, path)
    except Exception as exc:
        # The record is revoked (never usable again); repeating the idempotent
        # DELETE finishes the cleanup.
        log.exception("person refs: cleanup after revoking %s failed", consent_id)
        raise _error(
            502, "revoke_incomplete", "consent revoked but not fully deleted; retry"
        ) from exc
    return Response(status_code=204)
