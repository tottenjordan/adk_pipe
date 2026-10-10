"""Personalised variant previews REST API (owner side, UI only).

The owner re-renders one finished, uncast concept with one of their consented
people (``runserver/person_refs.py``) cast as the hero. A variant is a **fresh
render** (``creative_agent.render_concept``) of the concept's stored
``image_generation_prompt`` with the run's own product / logo / style
references, aspect ratio and rating-strictness flags, plus the person photo
(``ALLOW_ADULT``) and the post-render image check. It is a preview only: it is
never judged, rated, shared or deployed to a bandit experiment.

Storage is deliberately separate from the run's creatives:

- GCS: ``{gcs_folder}/{agent_output_dir}/variants/<owner slug>/<concept slug>/
  <key>.png`` (blob metadata ``consent_id``, ``Cache-Control: private,
  no-store``; ``/api/gcs`` serves ``variants/<slug>/`` paths to their owner
  only);
- state: ``person_variants[concept_name][key] = {status, gcs_uri, qa, attempts,
  consent_id, created_at, reason}``, written by ``state_delta`` events that
  carry ONLY ``person_variants`` (never ``final_visual_concepts``,
  ``generated_images`` or ``_generated_artifact_keys``).

``key = sha256(photo_uri | image_generation_prompt | image model)[:12]``: an
identical request whose render is ``done`` returns the cached variant.
Statuses: ``queued`` → ``rendering`` → ``done`` | ``failed`` | ``rejected``
(a safety filter blocked the person photo; unlike a base run there is no
person-less fallback, since the point of a variant is the person).

Quota: variants share the image model's ~2 images/min with live runs, so renders
run as detached tasks behind a process-wide ``asyncio.Semaphore``
(``VARIANT_RENDER_CONCURRENCY``, default 1) with a per-user daily cap
(``VARIANT_DAILY_CAP``, default 10; 429 ``variant_cap_reached``). The cap counts
renders started today by this api process for the user, or today's variants in
the session's state when that is higher. It is per process and resets on restart
(like the ``/runs`` duplicate guard, it assumes the single api instance).

Routes (user-scoped by path, gated by ``UserAuthzMiddleware`` like ``/shares``;
a foreign or unknown session is a 404):

- ``POST /variants/{user}/{app}/{session}``: body ``{concept_name,
  consent_id}`` → ``{key, status, concept_name, consent_id, cached, ...}``.
- ``GET /variants/{user}/{app}/{session}``: ``{variants: {concept: {key:
  record}}}``.

Person photo URIs are never logged or returned.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import hashlib
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException
from google.adk.events import Event, EventActions
from pydantic import BaseModel

from runserver import person_refs
from runserver.ratings import RATING_APPS, _as_obj, configured_report_bucket

log = logging.getLogger(__name__)

STATE_KEY = "person_variants"
VARIANT_APPS = RATING_APPS
STATUSES = ("queued", "rendering", "done", "failed", "rejected")
PENDING = ("queued", "rendering")
VARIANTS_SEGMENT = "variants"
VARIANT_CACHE_CONTROL = "private, no-store"
KEY_CHARS = 12
CONCEPT_NAME_MAX_CHARS = 512
DEFAULT_DAILY_CAP = 10
MAX_DAILY_CAP = 1000
DEFAULT_CONCURRENCY = 1
MAX_CONCURRENCY = 4
RUNSERVER_AUTHOR = "__runserver__"  # = async_runs.RUNSERVER_AUTHOR
_APP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")
# Output-folder prefixes a variant must never be written under.
_RESERVED_FIRST = ("person-refs", "shares")


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return max(low, min(high, value))


# --- Pure helpers -----------------------------------------------------------------


class VariantError(ValueError):
    def __init__(self, reason: str, message: str, status: int = 400):
        super().__init__(message)
        self.reason = reason
        self.status = status


def variant_key(photo_uri: str, prompt: str, model: str) -> str:
    """The cache key: ``sha256(photo_uri | prompt | model)[:12]``."""
    raw = f"{photo_uri}|{prompt}|{model}".encode()
    return hashlib.sha256(raw).hexdigest()[:KEY_CHARS]


def concept_slug(name: str) -> str:
    """A path-safe folder name for a concept (``[A-Za-z0-9_-]``, ≤ 80 chars)."""
    slug = _SLUG_RE.sub("_", name.strip()).strip("_")[:80]
    return slug or "concept"


def variant_object_path(
    state: Mapping[str, Any], owner: str, concept: str, key: str
) -> str:
    """``{gcs_folder}/{agent_output_dir}/variants/<owner slug>/<concept slug>/
    <key>.png``. ``gcs_folder`` / ``agent_output_dir`` come from (client-seedable)
    session state, so each must be one safe path segment, never ``variants`` or a
    reserved prefix; raises ``VariantError`` otherwise."""
    folder, subdir = state.get("gcs_folder"), state.get("agent_output_dir")
    for segment in (folder, subdir):
        if (
            not isinstance(segment, str)
            or not _SEGMENT_RE.match(segment)
            or segment == VARIANTS_SEGMENT
            or ".." in segment
        ):
            raise VariantError(
                "invalid_output_folder", "this run has no usable output folder"
            )
    if folder in _RESERVED_FIRST:
        raise VariantError(
            "invalid_output_folder", "this run has no usable output folder"
        )
    return (
        f"{folder}/{subdir}/{VARIANTS_SEGMENT}/{person_refs.slug_for(owner)}/"
        f"{concept_slug(concept)}/{key}.png"
    )


def find_concept(state: Mapping[str, Any], name: str) -> dict | None:
    """The concept named ``name`` in ``final_visual_concepts`` (a copy), or None."""
    from creative_agent.concept_guard import parse_concepts

    for concept in parse_concepts(state.get("final_visual_concepts")):
        if concept.get("concept_name") == name:
            return dict(concept)
    return None


def is_cast(state: Mapping[str, Any], name: str) -> bool:
    """Whether the run's base image for ``name`` already casts a person."""
    images = _as_obj(state.get("generated_images"))
    record = images.get(name) if isinstance(images, Mapping) else None
    return isinstance(record, Mapping) and record.get("cast") is True


def has_base_image(state: Mapping[str, Any], name: str) -> bool:
    """Whether the run rendered a base image for ``name`` (a ``gcs_uri``)."""
    images = _as_obj(state.get("generated_images"))
    record = images.get(name) if isinstance(images, Mapping) else None
    uri = record.get("gcs_uri") if isinstance(record, Mapping) else None
    return isinstance(uri, str) and bool(uri)


def variants_in(state: Mapping[str, Any]) -> dict[str, dict[str, dict]]:
    """``person_variants`` as ``{concept: {key: record}}`` (malformed entries
    dropped; a deep copy)."""
    raw = _as_obj(state.get(STATE_KEY))
    out: dict[str, dict[str, dict]] = {}
    if not isinstance(raw, Mapping):
        return out
    for concept, by_key in raw.items():
        if not isinstance(concept, str) or not isinstance(by_key, Mapping):
            continue
        records = {
            k: copy.deepcopy(dict(v))
            for k, v in by_key.items()
            if isinstance(k, str) and isinstance(v, Mapping)
        }
        if records:
            out[concept] = records
    return out


def count_created_on(state: Mapping[str, Any], day: str) -> int:
    """How many variants in ``state`` were created on ``day`` (``YYYY-MM-DD``)."""
    return sum(
        1
        for records in variants_in(state).values()
        for record in records.values()
        if str(record.get("created_at") or "").startswith(day)
    )


def variant_concept(concept: Mapping[str, Any]) -> dict:
    """The concept as a variant renders it: casting the person, with the
    reference wording appended when the prompt doesn't point at it yet."""
    from creative_agent.concept_guard import PERSON_HERO_LINE, PERSON_HERO_PHRASE

    out = dict(concept)
    prompt = str(out.get("image_generation_prompt") or "")
    if PERSON_HERO_PHRASE.lower() not in prompt.lower():
        prompt = prompt.rstrip() + PERSON_HERO_LINE
    out["image_generation_prompt"] = prompt
    out["casts_person_reference"] = True
    return out


def castable_reason(concept: Mapping[str, Any], safe_styles: Any) -> str | None:
    """Why the casting guard wouldn't cast a person in ``concept`` (its style
    isn't person-safe or its prompt has no human subject), or None."""
    from creative_agent.concept_guard import enforce_person_casting

    checked, warnings = enforce_person_casting(
        [variant_concept(concept)], available=True, max_cast=1, safe_styles=safe_styles
    )
    if checked[0].get("casts_person_reference") is True:
        return None
    text = warnings[0] if warnings else ""
    m = re.search(r"\(([^()]*)\)\s*$", text)
    return m.group(1) if m else "the casting guard refused it"


def _personal_uri(uri: str) -> bool:
    """A reference that points at a person photo or a variant (never re-used)."""
    segments = uri.split("/")
    return "person-refs" in segments or VARIANTS_SEGMENT in segments[:-1]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(now: dt.datetime) -> str:
    return now.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- Rendering (default renderer) ------------------------------------------------


@dataclass
class VariantRender:
    """A renderer's outcome: ``status`` is ``done`` (with image bytes),
    ``rejected`` or ``failed`` (with a ``reason``)."""

    status: str
    image_bytes: bytes | None = None
    mime: str = "image/png"
    qa: dict | None = None
    attempts: int = 0
    reason: str | None = None


Renderer = Callable[[Mapping[str, Any], dict, str], Awaitable[VariantRender]]


async def render_variant(
    state: Mapping[str, Any], concept: dict, photo_uri: str
) -> VariantRender:
    """Render ``concept`` with the person photo at ``photo_uri`` through
    ``creative_agent.render_concept`` (QA on, no person-less fallback)."""
    from creative_agent import image_tools
    from creative_agent.config import config
    from creative_agent.rating_signals import strictness_flags
    from creative_agent.references import resolve_references
    from creative_agent.render_concept import (
        fetch_person_photo,
        fetch_references,
        render_concept,
    )

    refs = [r for r in resolve_references(state) if not _personal_uri(r[0])]
    references = await fetch_references(refs)
    person = await fetch_person_photo(photo_uri)
    if person is None:
        return VariantRender("failed", reason="photo_unavailable")
    aspect_ratio = image_tools._resolve_aspect_ratio(
        concept,
        image_tools.valid_aspect_ratio_override(state.get("visual_aspect_ratio")),
        config.image_aspect_ratios_allowed,
        config.image_aspect_ratio_default,
    )
    result = await render_concept(
        variant_concept(concept),
        aspect_ratio=aspect_ratio,
        references=references,
        person=person,
        strictness=strictness_flags(state.get("rating_strictness")),
        qa=True,
        brand=str(state.get("brand") or ""),
        target_product=str(state.get("target_product") or ""),
        fallback_without_person=False,
    )
    if result.rejected_reason is not None:
        return VariantRender("rejected", reason=result.rejected_reason)
    if result.image_bytes is None:
        return VariantRender("failed", reason="no_image", attempts=result.attempts)
    return VariantRender(
        "done",
        image_bytes=result.image_bytes,
        mime=result.mime or "image/png",
        qa=result.qa,
        attempts=result.attempts,
    )


def _image_model() -> str:
    from creative_agent.config import config

    return str(config.image_gen_model)


def _safe_styles() -> Any:
    from creative_agent.config import config

    return config.person_safe_styles


def _run_is_live(app_name: str, user_id: str, session_id: str) -> bool:
    from runserver.async_runs import _is_live

    return _is_live((app_name, user_id, session_id))


# --- Module state (configure) ---------------------------------------------------

_SESSION_SERVICE: Any = None
_GCS_CLIENT: Any = None
_BUCKET: str | None = configured_report_bucket()
_RENDERER: Renderer = render_variant
_DAILY_CAP = _env_int("VARIANT_DAILY_CAP", DEFAULT_DAILY_CAP, 0, MAX_DAILY_CAP)
_CONCURRENCY = _env_int(
    "VARIANT_RENDER_CONCURRENCY", DEFAULT_CONCURRENCY, 1, MAX_CONCURRENCY
)
# Strong refs to detached render tasks (create_task keeps only a weak one), keyed
# by (app, user, session, concept, key) so a repeat POST joins the live render.
_TASKS: dict[tuple[str, str, str, str, str], asyncio.Task] = {}
# In-flight POSTs per (app, user, session, concept, consent): concurrent duplicates
# wait for the first one's response (claimed before any await).
_CLAIMS: dict[tuple, asyncio.Future] = {}
# Renders started per (user, UTC day) by this process (the daily cap).
_STARTED: dict[tuple[str, str], int] = {}
# One state writer per session at a time: each write re-reads person_variants.
_SESSION_LOCKS: dict[tuple[str, str, str], asyncio.Lock] = {}
_SEMAPHORE: tuple[Any, asyncio.Semaphore] | None = None


def configure(
    *,
    session_service,
    gcs_client: Any = None,
    bucket: str | None = None,
    renderer: Renderer | None = None,
    daily_cap: int | None = None,
    concurrency: int | None = None,
) -> None:
    """``bucket`` defaults to ``GOOGLE_CLOUD_STORAGE_BUCKET``; ``daily_cap`` /
    ``concurrency`` to ``VARIANT_DAILY_CAP`` / ``VARIANT_RENDER_CONCURRENCY``."""
    global _SESSION_SERVICE, _GCS_CLIENT, _BUCKET, _RENDERER, _DAILY_CAP
    global _CONCURRENCY, _SEMAPHORE
    _SESSION_SERVICE, _GCS_CLIENT = session_service, gcs_client
    _BUCKET = (bucket or "").strip().removeprefix("gs://").strip("/") or (
        configured_report_bucket()
    )
    _RENDERER = renderer or render_variant
    _DAILY_CAP = (
        _env_int("VARIANT_DAILY_CAP", DEFAULT_DAILY_CAP, 0, MAX_DAILY_CAP)
        if daily_cap is None
        else daily_cap
    )
    _CONCURRENCY = (
        _env_int("VARIANT_RENDER_CONCURRENCY", DEFAULT_CONCURRENCY, 1, MAX_CONCURRENCY)
        if concurrency is None
        else max(1, concurrency)
    )
    _SEMAPHORE = None
    _TASKS.clear()
    _CLAIMS.clear()
    _STARTED.clear()
    _SESSION_LOCKS.clear()


def _semaphore() -> asyncio.Semaphore:
    """The process-wide render semaphore (re-made for a new event loop)."""
    global _SEMAPHORE
    loop = asyncio.get_running_loop()
    if _SEMAPHORE is None or _SEMAPHORE[0] is not loop:
        _SEMAPHORE = (loop, asyncio.Semaphore(_CONCURRENCY))
    return _SEMAPHORE[1]


def _gcs() -> Any:
    if _GCS_CLIENT is not None:
        return _GCS_CLIENT
    from agent_common.clients import get_gcs_client

    return get_gcs_client()


def _upload(bucket_name: str, path: str, data: bytes, mime: str, consent_id: str):
    """Upload the variant with its consent id and a no-store cache (blocking)."""
    blob = _gcs().bucket(bucket_name).blob(path)
    blob.metadata = {"consent_id": consent_id}
    blob.cache_control = VARIANT_CACHE_CONTROL
    blob.upload_from_string(data, content_type=mime or "image/png")


async def _get_session(app_name: str, user_id: str, session_id: str):
    from google.adk.errors.session_not_found_error import SessionNotFoundError

    try:
        return await _SESSION_SERVICE.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
    except SessionNotFoundError:
        return None


async def _write_record(
    app_name: str, user_id: str, session_id: str, concept: str, key: str, record: dict
) -> None:
    """Merge one record into ``person_variants`` (fresh read, one event whose
    ``state_delta`` holds only ``person_variants``)."""
    lock = _SESSION_LOCKS.setdefault((app_name, user_id, session_id), asyncio.Lock())
    async with lock:
        session = await _get_session(app_name, user_id, session_id)
        if session is None:
            log.warning("variants: session %s vanished; dropping update", session_id)
            return
        variants = variants_in(session.state or {})
        variants.setdefault(concept, {})[key] = record
        event = Event(
            author=RUNSERVER_AUTHOR,
            invocation_id=RUNSERVER_AUTHOR,
            actions=EventActions(state_delta={STATE_KEY: variants}),
        )
        await _SESSION_SERVICE.append_event(session, event)


@dataclass
class _Job:
    app_name: str
    user_id: str
    session_id: str
    concept_name: str
    key: str
    consent_id: str
    photo_uri: str
    object_path: str
    concept: dict
    state: dict
    created_at: str


async def _run_job(job: _Job) -> None:
    """Render, upload and record one variant (never raises)."""
    base = {
        "consent_id": job.consent_id,
        "created_at": job.created_at,
        "gcs_uri": None,
        "qa": None,
        "attempts": 0,
        "reason": None,
    }

    async def write(**fields: Any) -> None:
        await _write_record(
            job.app_name,
            job.user_id,
            job.session_id,
            job.concept_name,
            job.key,
            {**base, **fields},
        )

    try:
        async with _semaphore():
            await write(status="rendering")
            outcome = await _RENDERER(job.state, job.concept, job.photo_uri)
            if outcome.status == "done" and outcome.image_bytes:
                # The consent may have been revoked while rendering: never store an
                # image made with a revoked consent.
                if (
                    await person_refs.active_consent(job.user_id, job.consent_id)
                    is None
                ):
                    await write(status="failed", reason="consent_revoked")
                    return
                bucket = _BUCKET or ""
                await asyncio.to_thread(
                    _upload,
                    bucket,
                    job.object_path,
                    outcome.image_bytes,
                    outcome.mime,
                    job.consent_id,
                )
                gcs_uri: str | None = f"gs://{bucket}/{job.object_path}"
            else:
                gcs_uri = None
        status = outcome.status if outcome.status in STATUSES else "failed"
        if status == "done" and gcs_uri is None:
            status = "failed"
        await write(
            status=status,
            gcs_uri=gcs_uri,
            qa=outcome.qa,
            attempts=outcome.attempts,
            reason=None if status == "done" else (outcome.reason or "no_image"),
        )
    except Exception:
        log.exception(
            "variants: render failed session=%s concept=%s key=%s",
            job.session_id,
            job.concept_name,
            job.key,
        )
        try:
            await write(status="failed", reason="render_error")
        except Exception:
            log.exception("variants: could not record the failure for %s", job.key)


# --- Routes -----------------------------------------------------------------------

router = APIRouter()


def _error(code: int, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=code, detail={"reason": reason, "message": message}
    )


class _VariantBody(BaseModel):
    # Typed loosely on purpose: bad values become a 400 with a reason (like
    # /shares), not a bare 422.
    concept_name: Any = None
    consent_id: Any = None


def _check_app(app_name: str) -> None:
    if not _APP_RE.match(app_name) or app_name not in VARIANT_APPS:
        raise _error(
            400, "invalid_app_name", f"app_name must be one of {list(VARIANT_APPS)}"
        )


def _public(concept: str, key: str, record: Mapping[str, Any], cached: bool) -> dict:
    return {"concept_name": concept, "key": key, "cached": cached, **record}


@router.post("/variants/{user_id}/{app_name}/{session_id}")
async def http_create_variant(
    user_id: str, app_name: str, session_id: str, body: _VariantBody
) -> dict:
    # Claimed synchronously, before the first await: a concurrent POST for the same
    # (session, concept, person) waits for this one and returns its response, so the
    # render starts (and counts against the cap) once.
    claim = (app_name, user_id, session_id, body.concept_name, body.consent_id)
    pending = _CLAIMS.get(claim) if _hashable(claim) else None
    if pending is not None:
        return await asyncio.shield(pending)
    if not _hashable(claim):
        return await _create_variant(user_id, app_name, session_id, body)
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _CLAIMS[claim] = future
    try:
        result = await _create_variant(user_id, app_name, session_id, body)
    except BaseException as exc:
        future.set_exception(exc)
        future.exception()  # retrieved: no "never retrieved" warning without waiters
        raise
    else:
        future.set_result(result)
        return result
    finally:
        if _CLAIMS.get(claim) is future:
            del _CLAIMS[claim]


def _hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


async def _create_variant(
    user_id: str, app_name: str, session_id: str, body: _VariantBody
) -> dict:
    _check_app(app_name)
    concept_name, consent_id = body.concept_name, body.consent_id
    if not (
        isinstance(concept_name, str)
        and 0 < len(concept_name) <= CONCEPT_NAME_MAX_CHARS
    ):
        raise _error(400, "invalid_concept_name", "concept_name is required")
    if not isinstance(consent_id, str) or not consent_id:
        raise _error(400, "invalid_consent_id", "consent_id is required")
    bucket = _BUCKET
    if not bucket:
        raise _error(503, "variants_unconfigured", "no variant bucket is configured")
    # A foreign session raises VertexAiSessionService's ownership ValueError, which
    # install_ownership_handler maps to 404 too.
    session = await _get_session(app_name, user_id, session_id)
    if session is None:
        raise _error(404, "session_not_found", "session not found")
    consent = await person_refs.active_consent(user_id, consent_id)
    if consent is None:
        raise _error(
            400, "consent_not_active", "that person isn't registered or was revoked"
        )
    photo_uri = str(consent.get("photo_uri") or "")
    if person_refs.photo_path(photo_uri, user_id, bucket) is None:
        raise _error(400, "consent_not_active", "that person's photo is unavailable")
    if _run_is_live(app_name, user_id, session_id):
        raise _error(409, "run_in_progress", "wait for the run to finish")
    state = dict(session.state or {})
    concept = find_concept(state, concept_name)
    if concept is None or not isinstance(concept.get("image_generation_prompt"), str):
        raise _error(400, "concept_not_found", "no such concept in this run")
    if not state.get("finalize_done") or not has_base_image(state, concept_name):
        raise _error(
            409,
            "concept_not_ready",
            "wait until the run has finished and this creative's image is rendered",
        )
    if is_cast(state, concept_name):
        raise _error(
            400,
            "concept_already_cast",
            "this creative already features a person; pick another one",
        )
    why = castable_reason(concept, _safe_styles())
    if why is not None:
        raise _error(400, "concept_not_castable", f"can't feature a person: {why}")
    key = variant_key(photo_uri, concept["image_generation_prompt"], _image_model())
    try:
        object_path = variant_object_path(state, user_id, concept_name, key)
    except VariantError as exc:
        raise _error(exc.status, exc.reason, str(exc)) from exc

    existing = variants_in(state).get(concept_name, {}).get(key)
    if existing and existing.get("status") == "done" and existing.get("gcs_uri"):
        return _public(concept_name, key, existing, cached=True)
    task_key = (app_name, user_id, session_id, concept_name, key)
    live = _TASKS.get(task_key)
    if live is not None and not live.done():
        record = existing or {"status": "queued", "consent_id": consent_id}
        return _public(concept_name, key, record, cached=False)

    now = _now()
    day = now.date().isoformat()
    started = max(_STARTED.get((user_id, day), 0), count_created_on(state, day))
    if started >= _DAILY_CAP:
        raise _error(
            429,
            "variant_cap_reached",
            f"at most {_DAILY_CAP} personalised previews a day; try again tomorrow",
        )
    _STARTED[(user_id, day)] = started + 1
    record = {
        "status": "queued",
        "consent_id": consent_id,
        "created_at": _iso(now),
        "gcs_uri": None,
        "qa": None,
        "attempts": 0,
        "reason": None,
    }
    job = _Job(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
        concept_name=concept_name,
        key=key,
        consent_id=consent_id,
        photo_uri=photo_uri,
        object_path=object_path,
        concept=concept,
        state=state,
        created_at=record["created_at"],
    )
    try:
        await _write_record(app_name, user_id, session_id, concept_name, key, record)
    except Exception as exc:
        _STARTED[(user_id, day)] = started
        log.exception("variants: could not queue %s", key)
        raise _error(502, "variant_failed", "could not queue the preview") from exc
    task = asyncio.create_task(_run_job(job))
    _TASKS[task_key] = task

    def _release(t: asyncio.Task) -> None:
        if _TASKS.get(task_key) is t:
            del _TASKS[task_key]

    task.add_done_callback(_release)
    return _public(concept_name, key, record, cached=False)


@router.get("/variants/{user_id}/{app_name}/{session_id}")
async def http_list_variants(user_id: str, app_name: str, session_id: str) -> dict:
    _check_app(app_name)
    session = await _get_session(app_name, user_id, session_id)
    if session is None:
        raise _error(404, "session_not_found", "session not found")
    variants = variants_in(session.state or {})
    # A pending record with no live task here was orphaned (an api restart):
    # report it failed so the panel stops polling and offers a retry.
    for concept, records in variants.items():
        for key, record in records.items():
            live = _TASKS.get((app_name, user_id, session_id, concept, key))
            if record.get("status") in PENDING and (live is None or live.done()):
                record["status"], record["reason"] = "failed", "interrupted"
    return {"variants": variants}
