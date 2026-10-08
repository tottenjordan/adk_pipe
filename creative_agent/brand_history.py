"""Brand history: what past runs for the same brand taught us (research F12).

Reads the brand's latest ``creative_evals`` rows (BigQuery) and their full eval
report JSON (GCS) and condenses them into a short note for the brief writer and
art director: styles used recently (to avoid repeating them), the strongest
styles/copy tones (to build on), recurring weak dimensions and frequently failed
compliance checks (to fix).

Advisory by design: every failure (unconfigured table, BigQuery/GCS errors,
oversized or malformed reports) degrades to less history, never to a failed
run. Report reads are restricted to ``gs://`` URIs in the configured bucket and
capped at ``REPORT_MAX_BYTES``.

Brief angle ids (``A1``..``A5``) are per-brief, so they are not aggregated
across runs; the copy's ``tone_style`` stands in for "what kind of copy worked".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from google.cloud import bigquery

from agent_common.clients import get_bigquery_client, get_gcs_client
from creative_eval.dimensions import (
    AD_COPY_GATES,
    DIMENSION_LABELS,
    VISUAL_GATES,
    gate_label,
)

from . import rating_signals
from .config import RATING_LEARNING_EFFECTS, config
from .prompt_safe import ALLOWED_TONES
from .prompt_safe import brace_free as _clean
from .prompt_safe import normalize_brand as normalize_brand  # re-export (facade)
from .style_shortlist import canonical_style, format_shortlist, pick_style_shortlist

logger = logging.getLogger(__name__)

# Shared lazy getters bound to the module's monkeypatch points.
_get_bigquery_client = get_bigquery_client
_get_gcs_client = get_gcs_client

REPORT_MAX_BYTES = 5 * 1024 * 1024
BQ_TIMEOUT_SECONDS = 8.0
# Per GCS call, and the overall budget for reading reports (from fetch start;
# later reports are skipped once it passes).
GCS_TIMEOUT_SECONDS = 3
REPORT_READ_DEADLINE_SECONDS = 8.0
# Styles from this many most-recent runs count as "used recently".
RECENT_STYLE_RUNS = 2
TOP_N = 3
MAX_WORDS = 120
# Upper bound on how long the history read may hold up the research fan-out.
BRAND_HISTORY_TIMEOUT_SECONDS = 10.0


# Only allowlisted values ever reach the prompts: canonical style families
# (canonical_style), the ad-copy tone Literal, known dimension labels and known
# gate names (rendered as their labels). Report/BQ strings are otherwise free
# text, so anything else is dropped.
ALLOWED_WEAKNESSES: frozenset[str] = frozenset(DIMENSION_LABELS.values())
ALLOWED_GATES: frozenset[str] = frozenset(AD_COPY_GATES + VISUAL_GATES)


def _canonical_styles(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        family = canonical_style(value)
        if family and family not in out:
            out.append(family)
    return out


def _allowed(values: Iterable[Any], allowed: frozenset[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value in allowed and value not in out:
            out.append(value)
    return out


def _allowed_counts(pairs: Any, allowed: frozenset[str]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for pair in pairs or []:
        try:
            name, n = pair
            n = int(n)
        except (TypeError, ValueError):
            continue
        if isinstance(name, str) and name in allowed and n > 0:
            out.append((name, n))
    return out


def _table_id() -> str | None:
    parts = [config.BQ_PROJECT_ID, config.BQ_DATASET_ID, config.BQ_TABLE_EVALS]
    present = [p for p in parts if p]
    return ".".join(present) if len(present) == len(parts) else None


def build_history_query(
    table: str, brand: str, limit: int
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """The parameterised SELECT for a brand's latest runs (pure).

    The brand is bound as ``@brand`` (case-insensitive exact match), never
    interpolated; ``table`` comes from config, not user input.
    """
    sql = f"""
        SELECT datetime, target_trend, overall_pass_rate, avg_ad_copy_score,
               avg_visual_score, weakest_dimension_labels, gates_pass_rate,
               eval_report_gcs_uri
        FROM `{table}`
        WHERE LOWER(TRIM(brand)) = LOWER(@brand)
        ORDER BY datetime DESC
        LIMIT @limit
    """
    params = [
        bigquery.ScalarQueryParameter("brand", "STRING", brand),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    return sql, params


def _report_location(uri: Any) -> tuple[str, str] | None:
    """``(bucket, object)`` for a ``gs://`` URI in the configured bucket, else None."""
    if not isinstance(uri, str) or not uri.startswith("gs://"):
        return None
    bucket, _, obj = uri[len("gs://") :].partition("/")
    if not obj or not config.GCS_BUCKET_NAME or bucket != config.GCS_BUCKET_NAME:
        return None
    return bucket, obj


def _read_report(gcs_client: Any, uri: Any) -> dict | None:
    """The eval report dict at ``uri``, or None (skipped/missing/invalid; logged)."""
    location = _report_location(uri)
    if location is None:
        if uri:
            logger.info("brand history: skipping report outside the bucket: %s", uri)
        return None
    bucket, obj = location
    try:
        blob = gcs_client.bucket(bucket).get_blob(obj, timeout=GCS_TIMEOUT_SECONDS)
        if blob is None:
            return None
        if (blob.size or 0) > REPORT_MAX_BYTES:
            logger.warning("brand history: report too large, skipped: %s", uri)
            return None
        report = json.loads(blob.download_as_bytes(timeout=GCS_TIMEOUT_SECONDS))
    except Exception as exc:
        logger.warning("brand history: could not read report %s: %s", uri, exc)
        return None
    return report if isinstance(report, dict) else None


def _row_get(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def _entries(report: Mapping[str, Any] | None, key: str) -> list[Mapping[str, Any]]:
    items = (report or {}).get(key)
    return (
        [e for e in items if isinstance(e, Mapping)] if isinstance(items, list) else []
    )


def _score(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    score = entry.get("score")
    return score if isinstance(score, Mapping) else {}


def _overall(entry: Mapping[str, Any]) -> float:
    try:
        return float(_score(entry).get("overall_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ranked_values(entries: Iterable[Mapping[str, Any]], field: str) -> list[Any]:
    """``field`` of the passing entries, best score first."""
    ranked = sorted(
        (e for e in entries if _score(e).get("passed")), key=_overall, reverse=True
    )
    return [e.get(field) for e in ranked]


def _failed_gates(report: Mapping[str, Any] | None) -> set[str]:
    """Names of the non-advisory gates that failed for any creative in a report."""
    return {
        str(g["gate"])
        for key in ("ad_copy_evaluations", "visual_concept_evaluations")
        for e in _entries(report, key)
        for g in _score(e).get("gates") or []
        if isinstance(g, Mapping)
        and g.get("gate") in ALLOWED_GATES
        and not g.get("passed")
        and not g.get("advisory")
    }


def aggregate_history(
    brand: str, rows: list[Any], reports: list[dict | None]
) -> dict[str, Any]:
    """Condense rows (latest first) + their reports (same order) (pure).

    ``reports`` holds None for a report that could not be read; ``reports``
    in the result counts the readable ones (the failed-check denominator).
    """
    recent_styles = _canonical_styles(
        e.get("visual_style")
        for report in reports[:RECENT_STYLE_RUNS]
        for e in _entries(report, "visual_concept_evaluations")
    )

    visuals = [e for r in reports for e in _entries(r, "visual_concept_evaluations")]
    copies = [e for r in reports for e in _entries(r, "ad_copy_evaluations")]

    weak = Counter(
        label.strip()
        for row in rows
        for label in str(_row_get(row, "weakest_dimension_labels") or "").split(",")
        if label.strip() in ALLOWED_WEAKNESSES
    )
    # Runs in which each non-advisory gate failed at least once.
    failed = Counter(gate for report in reports for gate in _failed_gates(report))
    return {
        "brand": brand,
        "runs": len(rows),
        "recent_styles": recent_styles,
        "reports": sum(1 for r in reports if r is not None),
        "strongest_styles": _canonical_styles(_ranked_values(visuals, "visual_style"))[
            :TOP_N
        ],
        "strongest_tones": _allowed(
            _ranked_values(copies, "tone_style"), ALLOWED_TONES
        )[:TOP_N],
        "weaknesses": weak.most_common(TOP_N),
        "failed_checks": failed.most_common(TOP_N),
    }


def fetch_brand_history(
    brand: str, *, limit: int = 5, bq_client: Any = None, gcs_client: Any = None
) -> dict[str, Any]:
    """Aggregated history of the brand's latest ``limit`` runs; ``{}`` when none.

    Fail-open: any error is logged as a warning and returns ``{}`` (a report
    that can't be read is skipped individually).
    """
    brand = (brand or "").strip()
    table = _table_id()
    if not brand or limit <= 0 or table is None:
        return {}
    started = time.monotonic()
    try:
        bq = bq_client or _get_bigquery_client()
        sql, params = build_history_query(table, brand, limit)
        job_config = bigquery.QueryJobConfig(
            query_parameters=params,
            job_timeout_ms=int(BQ_TIMEOUT_SECONDS * 1000),
        )
        rows = list(bq.query(sql, job_config=job_config).result())
        if not rows:
            return {}
        gcs = gcs_client
        reports: list[dict | None] = []
        for row in rows:
            if time.monotonic() - started >= REPORT_READ_DEADLINE_SECONDS:
                logger.warning("brand history: report deadline passed; skipping rest")
                reports.extend([None] * (len(rows) - len(reports)))
                break
            uri = _row_get(row, "eval_report_gcs_uri")
            if gcs is None and _report_location(uri) is not None:
                gcs = _get_gcs_client()
            reports.append(_read_report(gcs, uri) if gcs is not None else None)
        return aggregate_history(brand, rows, reports)
    except Exception as exc:
        logger.warning("brand history unavailable for %r: %s", brand, exc)
        return {}


def _join(items: Iterable[Any]) -> str:
    return ", ".join(c for c in (_clean(i) for i in items) if c)


def format_brand_history(history: Mapping[str, Any]) -> str:
    """The brand history as one short, brace-free note; ``""`` when empty."""
    runs = int(history.get("runs") or 0) if history else 0
    if runs <= 0:
        return ""
    reports = int(history.get("reports") or 0)
    parts: list[str] = []
    if styles := _join(_canonical_styles(history.get("recent_styles") or [])):
        parts.append(f"styles used recently: {styles}")
    strongest = [
        f"{joined} {label}"
        for joined, label in (
            (
                _join(_canonical_styles(history.get("strongest_styles") or [])),
                "visuals",
            ),
            (
                _join(_allowed(history.get("strongest_tones") or [], ALLOWED_TONES)),
                "copy tones",
            ),
        )
        if joined
    ]
    if strongest:
        parts.append("strongest: " + "; ".join(strongest))
    if weak := _join(
        f"{name} ({n} of {runs} runs)"
        for name, n in _allowed_counts(history.get("weaknesses"), ALLOWED_WEAKNESSES)
    ):
        parts.append(f"recurring weaknesses: {weak}")
    if checks := _join(
        f"{gate_label(name)} ({n} of {reports} runs)"
        for name, n in _allowed_counts(history.get("failed_checks"), ALLOWED_GATES)
        if reports > 0
    ):
        parts.append(f"checks often failed: {checks}")
    head = f"Recent runs for {_clean(history.get('brand') or 'this brand')} ({runs}): "
    tail = (
        " Build on what worked, fix the weaknesses, and avoid repeating the "
        "recent styles."
    )
    body = "; ".join(parts) + "."
    budget = MAX_WORDS - len(head.split()) - len(tail.split())
    words = body.split()
    if len(words) > budget:
        body = " ".join(words[: budget - 1]).rstrip(",;.") + " …"
    return head + body + tail


def _rating_learning_on(state: Mapping[str, Any]) -> bool:
    """Opted in for this run (exactly ``True``) and not globally switched off."""
    return state.get("learn_from_ratings") is True and config.rating_learning_enabled


# A fetch that raised or timed out (distinct from a legitimate None/{} result).
_FAILED = object()


async def _in_thread(fn: Any, timeout: float, label: str, brand: str, **kw: Any):
    """``fn(brand, **kw)`` in a worker thread, bounded; ``_FAILED`` on error."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, brand, **kw), timeout)
    except Exception as exc:  # incl. TimeoutError
        logger.warning("%s skipped for %r: %s", label, brand, exc or type(exc).__name__)
        return _FAILED


def _rating_delta(
    rows: Any, brand: str, *, keep_shortlist: bool
) -> tuple[dict[str, Any], list[str], list[str]]:
    """(state delta, styles to exclude, styles to prefer) from rating rows.

    ``rows`` is ``_FAILED``/None when the ratings were unavailable. Below
    ``rating_learning_min_ratings`` nothing is applied; otherwise each enabled
    effect writes its key and ``rating_signals_applied`` records what was used.
    """
    if rows is _FAILED or rows is None:
        return (
            {"rating_signals_applied": {"applied": False, "reason": "unavailable"}},
            [],
            [],
        )
    signals = rating_signals.aggregate_ratings(
        rows if isinstance(rows, list) else [],
        style_min=config.rating_style_min,
        reason_min=config.rating_reason_min,
    )
    n = signals["ratings"]
    if n < config.rating_learning_min_ratings:
        applied = {"ratings": n, "applied": False, "reason": "not_enough_ratings"}
        return {"rating_signals_applied": applied}, [], []
    effects = config.rating_learning_effects & RATING_LEARNING_EFFECTS
    if not effects:  # never "applied" with nothing applied
        applied = {"ratings": n, "applied": False, "reason": "no_effects"}
        return {"rating_signals_applied": applied}, [], []
    # A user style preference wins: the note names no style families then.
    note = (
        rating_signals.format_rating_signals(
            signals, brand, include_styles=not keep_shortlist
        )
        if "guidance" in effects
        else ""
    )
    strictness = list(signals["strictness"]) if "checks" in effects else []
    steer = "styles" in effects and not keep_shortlist
    excluded = list(signals["styles_excluded"]) if steer else []
    preferred = list(signals["styles_preferred"]) if steer else []
    delta: dict[str, Any] = {}
    if "guidance" in effects:
        delta["rating_signals"] = note
    if "checks" in effects:
        delta["rating_strictness"] = strictness
    delta["rating_signals_applied"] = {
        "ratings": n,
        "applied": True,
        "effects": sorted(effects),
        "signals": note,
        "strictness": strictness,
        "styles_excluded": excluded,
        "styles_preferred": preferred,
    }
    return delta, excluded, preferred


async def brand_history_state_delta(
    state: Mapping[str, Any],
    *,
    enabled: bool,
    runs: int,
    timeout: float = BRAND_HISTORY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """The state delta of the ``load_brand_history`` (learning context) node.

    Brand history: disabled (or ``runs <= 0``) → no query and no
    ``brand_history`` key. Otherwise ``brand_history`` is the formatted note
    (``""`` without history, on any error, or after ``timeout`` seconds).

    Rating learning (rating_signals.py): only when the run opted in
    (``learn_from_ratings is True``) and ``RATING_LEARNING_ENABLED`` is on, the
    brand's ratings are read *concurrently* with the history, under the same
    ``timeout`` (both in worker threads, so the event loop never blocks).
    Writes ``rating_signals`` (guidance), ``rating_strictness`` (checks) and
    ``rating_signals_applied``; see ``_rating_delta``. Off → no query and no
    rating keys.

    ``style_shortlist`` is drawn once, excluding the recent and the poorly
    rated styles and preferring the well-rated ones, unless the user set a
    ``visual_style_preference``.
    """
    history_on = enabled and runs > 0
    ratings_on = _rating_learning_on(state)
    if not history_on and not ratings_on:
        return {}
    brand = str(state.get("brand") or "")

    async def _skip() -> Any:
        return None

    history, rows = await asyncio.gather(
        _in_thread(fetch_brand_history, timeout, "brand history", brand, limit=runs)
        if history_on
        else _skip(),
        _in_thread(
            rating_signals.fetch_ratings,
            timeout,
            "rating learning",
            brand,
            days=config.rating_learning_window_days,
        )
        if ratings_on
        else _skip(),
    )
    keep_shortlist = bool(str(state.get("visual_style_preference") or "").strip())
    delta: dict[str, Any] = {}
    recent: list[str] = []
    if history_on:
        history = history if isinstance(history, Mapping) else {}
        delta["brand_history"] = format_brand_history(history)
        recent = [str(s) for s in history.get("recent_styles") or []]
    excluded: list[str] = []
    preferred: list[str] = []
    if ratings_on:
        rating_delta, excluded, preferred = _rating_delta(
            rows, brand, keep_shortlist=keep_shortlist
        )
        delta.update(rating_delta)
    if (recent or excluded or preferred) and not keep_shortlist:
        delta["style_shortlist"] = format_shortlist(
            pick_style_shortlist(
                exclude=frozenset(recent) | frozenset(excluded), prefer=preferred
            )
        )
    return delta
