"""Human creative ratings REST API (judge calibration, research F11).

A user rates a creative from a finished creative run (pass/fail, optional 1-5 score
and note) on the results page. Each rating snapshots the LLM judge's verdict for
the same creative (``judge_overall`` / ``judge_passed`` / ``judge_gates_passed`` /
``judge_model``, from the session's eval report) so ``runserver/calibration.py`` can
measure judge-human agreement without re-reading old reports.

Routes (all user-scoped by path, gated by ``UserAuthzMiddleware`` like
``/experiments/{user}/...``; a foreign or unknown session is a 404):

- ``PUT /ratings/{user}/{session}``: upsert one rating (body ``app_name``,
  ``creative_key``, ``kind``, ``verdict``, ``score?``, ``note?``).
- ``GET /ratings/{user}/{session}``: the user's ratings for that session.

``creative_key`` is ``visual:<concept_name>`` (kind ``visual``) or
``copy:<original_id>`` (kind ``ad_copy``) and must name a creative in the session's
state. The store is BigQuery or in memory (``RATINGS_STORE``, see
``runserver/ratings_store.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
_REPORT_CACHE_MAX = 64
_GS_RE = re.compile(r"^gs://(?P<bucket>[^/]+)/(?P<path>.+)$")

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
    if isinstance(score, Mapping):
        overall = score.get("overall_score")
        if isinstance(overall, (int, float)) and not isinstance(overall, bool):
            out["judge_overall"] = float(overall)
        if isinstance(score.get("passed"), bool):
            out["judge_passed"] = score["passed"]
    # Optional (creative_eval gates, absent from older reports).
    if isinstance(ev.get("gates_passed"), bool):
        out["judge_gates_passed"] = ev["gates_passed"]
    return out


class RatingError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def validate_rating(body: Mapping[str, Any]) -> dict[str, Any]:
    """Checked ``{app_name, creative_key, kind, verdict, score, note}`` (pure);
    raises ``RatingError`` for a bad field. Does not check the session."""
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
    return {
        "app_name": app_name,
        "creative_key": key,
        "kind": kind,
        "verdict": verdict,
        "score": score,
        "note": note,
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
            "created_at",
            "updated_at",
        )
        if k in row
    }


def gcs_report_loader(uri: str) -> Any:
    """Download + parse an eval report JSON from ``gs://`` (blocking)."""
    from agent_common.clients import get_gcs_client

    m = _GS_RE.match(uri)
    if not m:
        raise ValueError(f"not a gs:// uri: {uri!r}")
    blob = get_gcs_client().bucket(m["bucket"]).blob(m["path"])
    return json.loads(blob.download_as_text())


# --- Module state (configure) ---------------------------------------------------

_SESSION_SERVICE: Any = None
_STORE: Any = InMemoryRatingsStore()
_REPORT_LOADER: ReportLoader = gcs_report_loader
# Eval reports are written once at the end of a run: cache the parsed JSON by URI.
_REPORT_CACHE: OrderedDict[str, Any] = OrderedDict()


def configure(
    *,
    session_service,
    store,
    report_loader: ReportLoader | None = None,
) -> None:
    global _SESSION_SERVICE, _STORE, _REPORT_LOADER
    _SESSION_SERVICE, _STORE = session_service, store
    _REPORT_LOADER = report_loader or gcs_report_loader
    _REPORT_CACHE.clear()


async def load_report(state: Mapping[str, Any]) -> Any:
    """The session's eval report: state first, else its GCS JSON. Fail soft: a
    missing/unreadable report gives None (the rating is stored without judge
    fields rather than refused)."""
    report = _as_obj(state.get(REPORT_KEY))
    if isinstance(report, Mapping):
        return report
    uri = state.get(REPORT_URI_KEY)
    if not isinstance(uri, str) or not uri.startswith("gs://"):
        return None
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
    note: Any = None


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
    now = utcnow()
    row = {
        "rating_id": rating_id(session_id, fields["creative_key"], user_id),
        "session_id": session_id,
        "user_id": user_id,
        **fields,
        **judge_fields(await load_report(state), info),
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


@router.get("/ratings/{user_id}/{session_id}")
async def http_list_ratings(user_id: str, session_id: str) -> dict:
    rows = await _STORE.list_for_session(user_id, session_id)
    return {"ratings": [to_public(r) for r in rows]}
