"""Human creative ratings REST API (judge calibration, research F11).

A user rates a creative from a finished creative run (pass/fail, optional 1-5 score
and note) on the results page. Each rating snapshots the LLM judge's verdict for
the same creative (``judge_overall`` / ``judge_passed`` / ``judge_gates_passed`` /
``judge_model``, from the session's eval report) so ``runserver/calibration.py`` can
measure judge-human agreement without re-reading old reports.

Routes (all user-scoped by path, gated by ``UserAuthzMiddleware`` like
``/experiments/{user}/...``; a foreign or unknown session is a 404):

- ``PUT /ratings/{user}/{session}``: upsert one rating (body ``app_name``,
  ``creative_key``, ``kind``, ``verdict``, ``score?``, ``note?``,
  ``fail_reasons?``: allowlisted chips from ``runserver/rating_reasons.py``,
  emptied on a pass).
- ``GET /ratings/{user}/{session}``: the user's ratings for that session.
- ``GET /ratings/{user}/calibration``: judge-human agreement over all the user's
  ratings (``runserver/calibration.py``).

``creative_key`` is ``visual:<concept_name>`` (kind ``visual``) or
``copy:<original_id>`` (kind ``ad_copy``) and must name a creative in the session's
state. The store is BigQuery or in memory (``RATINGS_STORE``, see
``runserver/ratings_store.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from runserver.calibration import calibration_report
from runserver.rating_reasons import FAIL_REASONS, FAIL_REASONS_BY_KIND
from runserver.ratings_store import InMemoryRatingsStore, rating_id, utcnow

log = logging.getLogger(__name__)

RATING_APPS = ("creative_agent", "interactive_creative")
KIND_PREFIXES = {"visual": "visual:", "ad_copy": "copy:"}
VERDICTS = ("pass", "fail")
SCORE_RANGE = (1, 5)
NOTE_MAX_CHARS = 2000
CREATIVE_KEY_MAX_CHARS = 512
REPORT_KEY = "creative_evaluation_report"
REPORT_URI_KEY = "eval_report_gcs_uri"
REPORT_SUFFIX = "/creative_eval_report.json"
REPORT_MAX_BYTES = 5 * 1024 * 1024
_REPORT_CACHE_MAX = 64
_GS_RE = re.compile(r"^gs://(?P<bucket>[^/]+)/(?P<path>.+)$")
# Brief angle ids are 'A1'..'A5' (CreativeBrief); anything else is not stamped.
_ANGLE_RE = re.compile(r"^A\d{1,2}$")

ReportLoader = Callable[[str], Any]


# --- Pure helpers -----------------------------------------------------------------


def _as_obj(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _items(container: Any, key: str) -> list[dict]:
    obj = _as_obj(container)
    if isinstance(obj, Mapping):
        obj = obj.get(key)
    return [x for x in obj if isinstance(x, Mapping)] if isinstance(obj, list) else []


def _id_text(value: Any) -> str | None:
    """``original_id`` as the frontend renders it (``1``, not ``1.0``)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (int, str)) and str(value).strip():
        return str(value).strip()
    return None


def creative_index(state: Mapping[str, Any]) -> dict[str, dict]:
    """``{creative_key: {kind, name|original_id, headline}}`` for every creative in
    the session state (final visual concepts + final ad copies)."""
    out: dict[str, dict] = {}
    for vc in _items(state.get("final_visual_concepts"), "visual_concepts"):
        name = vc.get("concept_name")
        if isinstance(name, str) and name.strip():
            out[f"visual:{name}"] = {"kind": "visual", "name": name}
    for ac in _items(state.get("ad_copy_critique"), "ad_copies"):
        if (oid := _id_text(ac.get("original_id"))) is not None:
            out[f"copy:{oid}"] = {
                "kind": "ad_copy",
                "original_id": oid,
                "headline": ac.get("headline"),
            }
    return out


def _creative_item(
    state: Mapping[str, Any], kind: str, creative_key: str
) -> Mapping[str, Any] | None:
    """The state item a ``creative_key`` names (first match), or None."""
    ident = creative_key.split(":", 1)[1] if ":" in creative_key else ""
    if kind == "visual":
        items = _items(state.get("final_visual_concepts"), "visual_concepts")
        return next((v for v in items if v.get("concept_name") == ident), None)
    items = _items(state.get("ad_copy_critique"), "ad_copies")
    return next((a for a in items if _id_text(a.get("original_id")) == ident), None)


def learning_context(
    state: Mapping[str, Any], kind: str, creative_key: str
) -> dict[str, str]:
    """The rating row's learning-context columns, from the session state (pure).

    Allowlisted values only, so a later learning step can never carry model or
    user free text into a prompt: ``visual_style`` is a canonical style family
    (``canonical_style``), ``tone_style`` one of the ``FinalAdCopy`` tones,
    ``angle_id`` a brief angle id (``A1``..). Anything else is ``""``. ``brand``
    is trimmed + lower-cased like brand_history's match, and is only ever used as
    a parameterised lookup key (``normalize_brand``, shared with brand_history)."""
    # Lazy: importing creative_agent builds its agent graph (like async_runs).
    from creative_agent import AD_COPY_TONES, canonical_style, normalize_brand

    out = {
        "brand": normalize_brand(state.get("brand")),
        "visual_style": "",
        "tone_style": "",
        "angle_id": "",
    }
    item = _creative_item(state, kind, creative_key)
    if item is None:
        return out
    angle = item.get("angle_id")
    if isinstance(angle, str) and _ANGLE_RE.match(angle.strip()):
        out["angle_id"] = angle.strip()
    if kind == "visual":
        out["visual_style"] = canonical_style(item.get("visual_style")) or ""
    elif (tone := item.get("tone_style")) in AD_COPY_TONES:
        out["tone_style"] = str(tone)
    return out


def _find_eval(report: Mapping[str, Any], info: Mapping[str, Any]) -> Mapping | None:
    """The judge's evaluation of one creative, or None when it can't be matched
    unambiguously (a wrong match would corrupt the calibration, a miss only
    drops one pair)."""
    if info["kind"] == "visual":
        hits = [
            e
            for e in _items(report, "visual_concept_evaluations")
            if e.get("concept_name") == info["name"]
        ]
        return hits[0] if len(hits) == 1 else None
    evals = _items(report, "ad_copy_evaluations")
    # Headline first: the judge often echoes the same original_id for every copy.
    headline = info.get("headline")
    if headline:
        hits = [e for e in evals if e.get("headline") == headline]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None
    hits = [e for e in evals if _id_text(e.get("original_id")) == info["original_id"]]
    return hits[0] if len(hits) == 1 else None


def judge_fields(report: Any, info: Mapping[str, Any]) -> dict[str, Any]:
    """The judge columns of a rating row (all None when unavailable)."""
    out: dict[str, Any] = {
        "judge_overall": None,
        "judge_passed": None,
        "judge_gates_passed": None,
        "judge_model": None,
    }
    report = _as_obj(report)
    if not isinstance(report, Mapping):
        return out
    model = report.get("judge_model")
    out["judge_model"] = model if isinstance(model, str) and model else None
    ev = _find_eval(report, info)
    if ev is None:
        return out
    score = ev.get("score")
    if not isinstance(score, Mapping):
        return out
    overall = score.get("overall_score")
    if isinstance(overall, (int, float)) and not isinstance(overall, bool):
        out["judge_overall"] = float(overall)
    if isinstance(score.get("passed"), bool):
        out["judge_passed"] = score["passed"]
    # creative_eval gates (CreativeScore.gates / gates_passed). Only trusted when
    # gates were actually recorded: gates_passed defaults to True, so a gate-less
    # (pre-gates) report would otherwise read as "every gate passed".
    gates = score.get("gates")
    if (
        isinstance(gates, list)
        and gates
        and isinstance(score.get("gates_passed"), bool)
    ):
        out["judge_gates_passed"] = score["gates_passed"]
    return out


class RatingError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def validate_fail_reasons(value: Any, verdict: str, kind: str) -> list[str]:
    """Allowlisted fail-reason chips, deduped in order; ``[]`` on a pass (pure).

    An unknown value is a 400; a known reason that doesn't apply to ``kind``
    (``FAIL_REASONS_BY_KIND``, the chips the UI offers) is dropped silently."""
    if value is None:
        value = []
    if not isinstance(value, list) or not all(
        isinstance(r, str) and r in FAIL_REASONS for r in value
    ):
        raise RatingError(
            "invalid_fail_reasons", f"fail_reasons must be a list of {FAIL_REASONS}"
        )
    if verdict == "pass":
        return []
    applicable = FAIL_REASONS_BY_KIND[kind]
    return [r for r in dict.fromkeys(value) if r in applicable]


def validate_rating(body: Mapping[str, Any]) -> dict[str, Any]:
    """Checked ``{app_name, creative_key, kind, verdict, score, note,
    fail_reasons}`` (pure); raises ``RatingError`` for a bad field. Does not check
    the session."""
    app_name = body.get("app_name")
    if app_name not in RATING_APPS:
        raise RatingError("invalid_app_name", f"app_name must be one of {RATING_APPS}")
    kind = body.get("kind")
    if kind not in KIND_PREFIXES:
        raise RatingError("invalid_kind", f"kind must be one of {list(KIND_PREFIXES)}")
    key = body.get("creative_key")
    if (
        not isinstance(key, str)
        or len(key) > CREATIVE_KEY_MAX_CHARS
        or not key.startswith(KIND_PREFIXES[kind])
        or not key[len(KIND_PREFIXES[kind]) :].strip()
    ):
        raise RatingError(
            "invalid_creative_key",
            f"creative_key must be '{KIND_PREFIXES[kind]}<id>' for kind {kind}",
        )
    verdict = body.get("verdict")
    if verdict not in VERDICTS:
        raise RatingError("invalid_verdict", f"verdict must be one of {VERDICTS}")
    score = body.get("score")
    if score is not None and (
        isinstance(score, bool)
        or not isinstance(score, int)
        or not SCORE_RANGE[0] <= score <= SCORE_RANGE[1]
    ):
        raise RatingError(
            "invalid_score",
            f"score must be an integer {SCORE_RANGE[0]}-{SCORE_RANGE[1]}",
        )
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise RatingError("invalid_note", "note must be a string")
    note = (note or "").strip() or None
    if note is not None and len(note) > NOTE_MAX_CHARS:
        raise RatingError("invalid_note", f"note must be ≤ {NOTE_MAX_CHARS} characters")
    fail_reasons = validate_fail_reasons(body.get("fail_reasons"), verdict, kind)
    return {
        "app_name": app_name,
        "creative_key": key,
        "kind": kind,
        "verdict": verdict,
        "score": score,
        "note": note,
        "fail_reasons": fail_reasons,
    }


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def to_public(row: Mapping[str, Any]) -> dict:
    """A rating as the API returns it (no ``user_id``; ISO timestamps)."""
    return {
        k: _iso(row.get(k))
        for k in (
            "rating_id",
            "session_id",
            "app_name",
            "creative_key",
            "kind",
            "verdict",
            "score",
            "note",
            "judge_overall",
            "judge_passed",
            "judge_gates_passed",
            "judge_model",
            "judge_source",
            "fail_reasons",
            "created_at",
            "updated_at",
        )
        if k in row
    }


def configured_report_bucket(env: Mapping[str, str] = os.environ) -> str | None:
    """The run-output bucket eval reports live in (``GOOGLE_CLOUD_STORAGE_BUCKET``,
    the var deploy ships; local fallback ``GCS_BUCKET_NAME``)."""
    raw = env.get("GOOGLE_CLOUD_STORAGE_BUCKET") or env.get("GCS_BUCKET_NAME") or ""
    return raw.strip().removeprefix("gs://").strip("/") or None


def allowed_report_uri(uri: Any, bucket: str | None) -> bool:
    """Only ``gs://<configured bucket>/.../creative_eval_report.json``.

    ``eval_report_gcs_uri`` is session state, which a client can seed via
    createSession, so it must never be able to point the api at an arbitrary
    object (or at a bucket the api SA happens to read)."""
    if not (isinstance(uri, str) and bucket):
        return False
    m = _GS_RE.match(uri)
    if not m or m["bucket"] != bucket:
        return False
    path = m["path"]
    return path.endswith(REPORT_SUFFIX) and ".." not in path.split("/")


def gcs_report_loader(uri: str) -> Any:
    """Download + parse an eval report JSON from ``gs://`` (blocking); refuses
    objects over ``REPORT_MAX_BYTES``."""
    from agent_common.clients import get_gcs_client

    m = _GS_RE.match(uri)
    if not m:
        raise ValueError(f"not a gs:// uri: {uri!r}")
    blob = get_gcs_client().bucket(m["bucket"]).blob(m["path"])
    blob.reload()
    if blob.size is None or blob.size > REPORT_MAX_BYTES:
        raise ValueError(
            f"eval report {uri} is {blob.size} bytes (max {REPORT_MAX_BYTES})"
        )
    return json.loads(blob.download_as_text())


# --- Module state (configure) ---------------------------------------------------

_SESSION_SERVICE: Any = None
_STORE: Any = InMemoryRatingsStore()
_REPORT_LOADER: ReportLoader = gcs_report_loader
_REPORT_BUCKET: str | None = configured_report_bucket()
# Eval reports are written once at the end of a run: cache the parsed JSON by URI.
_REPORT_CACHE: OrderedDict[str, Any] = OrderedDict()


def configure(
    *,
    session_service,
    store,
    report_loader: ReportLoader | None = None,
    report_bucket: str | None = None,
) -> None:
    """``report_bucket`` defaults to ``configured_report_bucket()``."""
    global _SESSION_SERVICE, _STORE, _REPORT_LOADER, _REPORT_BUCKET
    _SESSION_SERVICE, _STORE = session_service, store
    _REPORT_LOADER = report_loader or gcs_report_loader
    _REPORT_BUCKET = report_bucket or configured_report_bucket()
    _REPORT_CACHE.clear()


async def _load_gcs_report(uri: str) -> Mapping | None:
    if uri in _REPORT_CACHE:
        _REPORT_CACHE.move_to_end(uri)
        return _REPORT_CACHE[uri]
    try:
        report = await asyncio.to_thread(_REPORT_LOADER, uri)
    except Exception:
        log.warning("ratings: could not load eval report %s", uri, exc_info=True)
        return None
    if not isinstance(report, Mapping):
        return None
    _REPORT_CACHE[uri] = report
    while len(_REPORT_CACHE) > _REPORT_CACHE_MAX:
        _REPORT_CACHE.popitem(last=False)
    return report


async def load_report(state: Mapping[str, Any]) -> tuple[Mapping | None, str]:
    """``(report, judge_source)`` for the session.

    The GCS report the run wrote (``eval_report_gcs_uri``, only under the
    configured bucket with the report filename) wins: ``judge_source="gcs"``.
    Otherwise the state copy (``"state"``; client-seedable, so the calibration
    script leaves these out by default), else ``(None, "none")``. Fail soft: an
    unreadable report gives no judge fields rather than a refused rating."""
    uri = state.get(REPORT_URI_KEY)
    if allowed_report_uri(uri, _REPORT_BUCKET):
        report = await _load_gcs_report(str(uri))
        if report is not None:
            return report, "gcs"
    elif uri:
        log.warning("ratings: ignoring eval_report_gcs_uri outside the report bucket")
    report = _as_obj(state.get(REPORT_KEY))
    if isinstance(report, Mapping):
        return report, "state"
    return None, "none"


# --- Routes -----------------------------------------------------------------------

router = APIRouter()


def _error(code: int, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=code, detail={"reason": reason, "message": message}
    )


class _RatingBody(BaseModel):
    # Typed loosely on purpose: validate_rating turns bad values into a 400 with a
    # reason (like /experiments), not a bare 422.
    app_name: Any = None
    creative_key: Any = None
    kind: Any = None
    verdict: Any = None
    score: Any = None
    # Typed (bounds the body); the 2000-char business cap is in validate_rating.
    note: str | None = Field(default=None, max_length=4000)
    # Bounded here; items are checked against FAIL_REASONS in validate_rating.
    fail_reasons: list[Any] | None = Field(default=None, max_length=len(FAIL_REASONS))


async def _get_session(app_name: str, user_id: str, session_id: str):
    from google.adk.errors.session_not_found_error import SessionNotFoundError

    try:
        return await _SESSION_SERVICE.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
    except SessionNotFoundError:
        return None


@router.put("/ratings/{user_id}/{session_id}")
async def http_put_rating(user_id: str, session_id: str, body: _RatingBody) -> dict:
    try:
        fields = validate_rating(body.model_dump())
    except RatingError as exc:
        raise _error(400, exc.reason, str(exc)) from exc
    # A foreign session raises VertexAiSessionService's ownership ValueError, which
    # install_ownership_handler maps to 404 too.
    session = await _get_session(fields["app_name"], user_id, session_id)
    if session is None:
        raise _error(404, "session_not_found", "session not found")
    state = dict(session.state or {})
    info = creative_index(state).get(fields["creative_key"])
    if info is None:
        raise _error(400, "unknown_creative_key", "creative_key is not in this run")
    report, judge_source = await load_report(state)
    now = utcnow()
    row = {
        "rating_id": rating_id(session_id, fields["creative_key"], user_id),
        "session_id": session_id,
        "user_id": user_id,
        **fields,
        **judge_fields(report, info),
        "judge_source": judge_source if report is not None else "none",
        **learning_context(state, fields["kind"], fields["creative_key"]),
        "created_at": now,
        "updated_at": now,
    }
    try:
        await _STORE.upsert(row)
    except Exception as exc:
        log.exception("ratings: upsert %s failed", row["rating_id"])
        raise _error(502, "store_failed", "could not save the rating") from exc
    out = to_public(row)
    out.pop("created_at", None)  # the stored value is kept on update; not known here
    return out


async def _read(coro) -> list[dict]:
    try:
        return await coro
    except Exception as exc:
        log.exception("ratings: store read failed")
        raise _error(502, "store_failed", "could not read ratings") from exc


# Declared before the session route so "calibration" is never read as a session id.
@router.get("/ratings/{user_id}/calibration")
async def http_calibration(user_id: str) -> dict:
    """Judge-human agreement over every rating by the user (runserver/calibration.py)."""
    return calibration_report(await _read(_STORE.list_for_user(user_id)))


@router.get("/ratings/{user_id}/{session_id}")
async def http_list_ratings(user_id: str, session_id: str) -> dict:
    rows = await _read(_STORE.list_for_session(user_id, session_id))
    return {"ratings": [to_public(r) for r in rows]}
