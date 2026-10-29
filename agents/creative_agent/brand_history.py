"""Brand history: what past runs for the same brand taught us (research F12).

Reads the brand's latest ``creative_evals`` rows (BigQuery) and their full eval
report JSON (GCS) and condenses them into a short note for the brief writer and
art director: styles used recently (to favour fresh ones), the strongest
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
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
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
RATINGS_HISTORY_LIMIT = 50
# Upper bound on how long the history read may hold up the research fan-out.
BRAND_HISTORY_TIMEOUT_SECONDS = 10.0


# Only allowlisted values ever reach the prompts: canonical style families
# (canonical_style), the ad-copy tone Literal, known dimension labels, known
# gate names (rendered as their labels), and canonical human-reviewer rules.
# Report/BQ strings and reviewer notes are otherwise free text, so anything else
# is dropped (zero characters of user/reviewer free text ever reach the prompt).
ALLOWED_WEAKNESSES: frozenset[str] = frozenset(DIMENSION_LABELS.values())
ALLOWED_GATES: frozenset[str] = frozenset(AD_COPY_GATES + VISUAL_GATES)
ALLOWED_HUMAN_RULES: tuple[str, ...] = (
    "preserve exact product geometry",
    "keep text minimal and legible",
    "keep hands off product packaging",
    "follow brand color palette",
    "make trend connection unmistakable",
)
ALLOWED_HUMAN_RULES_SET: frozenset[str] = frozenset(ALLOWED_HUMAN_RULES)

_HUMAN_RULE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "preserve exact product geometry",
        re.compile(
            r"\b("
            r"geometry|shapes?|packaging\s+distort\w*|distort\w*|"
            r"warped|deform\w*|morph\w*|misshapen|wrong\s+product|wrong\s+bottle|"
            r"bottle\s+(?:shape|geometry|distort\w*|warped|deform\w*|morph\w*|wrong|different|proportions?)|"
            r"proportions?|silhouettes?|labels?\s+(?:wrong|distort\w*|missing|changed)|"
            r"product\s+looks?\s+(?:wrong|fake|different)"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        "keep text minimal and legible",
        re.compile(
            r"\b("
            r"texts?|fonts?|typos?|misspell\w*|spelling|gibberish|"
            r"illegible|unreadable|garbled|wordy|too\s+many\s+words|"
            r"caption\s+overlays?|lettering|kerning"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        "keep hands off product packaging",
        re.compile(
            r"\b("
            r"hands?|fingers?|thumbs?|holding|gripping|grasping|"
            r"covering\s+(?:the\s+)?(?:pack\w*|product|bottle|label)|"
            r"obscuring\s+(?:the\s+)?(?:pack\w*|product|bottle|label)"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        "follow brand color palette",
        re.compile(
            r"\b("
            r"colors?|colours?|palettes?|hex|off-brand\s+hues?|hues?|"
            r"tints?|wrong\s+shade|brand\s+tones?"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        "make trend connection unmistakable",
        re.compile(
            r"\b("
            r"trends?|motifs?|generic|disconnected|forced\s+bridge|"
            r"unrelated|off-trend|weak\s+hook"
            r")\b",
            re.IGNORECASE,
        ),
    ),
)


def classify_human_feedback_tags(note: Any) -> list[str]:
    """Deterministically map a reviewer note to canonical ``ALLOWED_HUMAN_RULES`` tags.

    Security invariant: zero characters from ``note`` are ever copied, sliced, or
    interpolated into the returned list — only literal constants from
    ``ALLOWED_HUMAN_RULES`` can be returned.
    """
    if note is None:
        return []
    raw = note if isinstance(note, str) else str(note)
    if not raw.strip():
        return []
    return [rule for rule, pattern in _HUMAN_RULE_PATTERNS if pattern.search(raw)]


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


def _ratings_table_id() -> str | None:
    parts = [config.BQ_PROJECT_ID, config.BQ_DATASET_ID, config.BQ_TABLE_RATINGS]
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
        SELECT datetime, overall_pass_rate, avg_ad_copy_score,
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


def build_ratings_history_query(
    ratings_table: str, evals_table: str, brand: str, limit: int = RATINGS_HISTORY_LIMIT
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """Parameterised SELECT joining a brand's human ratings to eval rows (pure).

    Uses the byte-identical ``stable_row_id(session_id, "eval")`` SHA-256 join
    expression (``json.dumps([session_id, "eval"])`` → ``["<session_id>", "eval"]``,
    8-char lowercase hex prefix) guarded by ``^[A-Za-z0-9_-]{1,128}$``.
    """
    sql = f"""
        SELECT r.session_id, r.creative_key, r.kind, r.verdict, r.score, r.note,
               e.eval_report_gcs_uri
        FROM `{ratings_table}` AS r
        INNER JOIN `{evals_table}` AS e
          ON REGEXP_CONTAINS(r.session_id, r'^[A-Za-z0-9_-]{{1,128}}$')
         AND SUBSTR(TO_HEX(SHA256(CONCAT('["', r.session_id, '", "eval"]'))), 1, 8) = e.uuid
        WHERE LOWER(TRIM(e.brand)) = LOWER(@brand)
        ORDER BY r.updated_at DESC
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
    if (
        not obj
        or ".." in obj.split("/")
        or not config.GCS_BUCKET_NAME
        or bucket != config.GCS_BUCKET_NAME
    ):
        return None
    return bucket, obj


def _read_report(
    gcs_client: Any,
    uri: Any,
    *,
    timeout: int | float = GCS_TIMEOUT_SECONDS,
) -> dict | None:
    """The eval report dict at ``uri``, or None (skipped/missing/invalid; logged)."""
    location = _report_location(uri)
    if location is None:
        if uri:
            logger.info("brand history: skipping report outside the bucket: %s", uri)
        return None
    bucket, obj = location
    try:
        blob = gcs_client.bucket(bucket).get_blob(obj, timeout=timeout)
        if blob is None:
            return None
        if (blob.size or 0) > REPORT_MAX_BYTES:
            logger.warning("brand history: report too large, skipped: %s", uri)
            return None
        report = json.loads(blob.download_as_bytes(timeout=timeout))
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


def _rating_weight(verdict: str, score: Any) -> int:
    if verdict == "pass":
        try:
            if score is not None and float(score) >= 4.0:
                return 2
        except (TypeError, ValueError):
            pass
        return 1
    return -1


def _merge_human_ranked(
    base: list[str], human_net: Mapping[str, int], human_order: Sequence[str]
) -> list[str]:
    """Boost net-positive human-rated items ahead of ``base`` and demote net-negative ones."""
    if not human_net:
        return base[:TOP_N]
    order_index = {item: idx for idx, item in enumerate(human_order)}
    boosted = sorted(
        (item for item, net in human_net.items() if net > 0),
        key=lambda item: (-human_net[item], order_index.get(item, 9999)),
    )
    out: list[str] = list(boosted)
    for item in base:
        if human_net.get(item, 0) < 0:
            continue
        if item not in out:
            out.append(item)
    return out[:TOP_N]


def _id_text(value: Any) -> str | None:
    """Canonical string id (normalising integer floats like ``2.0 -> "2"``; rejecting ``bool``)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _match_rated_style_or_tone(
    report: Mapping[str, Any] | None, creative_key: str
) -> tuple[str | None, str | None]:
    """Return ``(canonical_style, allowed_tone)`` when ``creative_key`` matches unambiguously."""
    if not isinstance(report, Mapping):
        return None, None
    visual_hits = [
        entry
        for entry in _entries(report, "visual_concept_evaluations")
        if (concept_name := str(entry.get("concept_name") or "").strip())
        and creative_key in (concept_name, f"visual:{concept_name}")
    ]
    if visual_hits:
        if len(visual_hits) != 1:
            return None, None
        return canonical_style(visual_hits[0].get("visual_style")), None

    copy_hits = [
        entry
        for entry in _entries(report, "ad_copy_evaluations")
        if (oid := _id_text(entry.get("original_id"))) is not None
        and creative_key in (oid, f"copy:{oid}")
    ]
    if len(copy_hits) == 1:
        tone = copy_hits[0].get("tone_style")
        allowed_tone = tone if isinstance(tone, str) and tone in ALLOWED_TONES else None
        return None, allowed_tone
    return None, None


def aggregate_history(
    brand: str,
    rows: list[Any],
    reports: list[dict | None],
    *,
    ratings: Iterable[Any] = (),
    reports_by_uri: Mapping[str, dict | None] | None = None,
) -> dict[str, Any]:
    """Condense rows (latest first) + their reports (same order) + human ratings (pure).

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

    base_styles = _canonical_styles(_ranked_values(visuals, "visual_style"))
    base_tones = _allowed(_ranked_values(copies, "tone_style"), ALLOWED_TONES)

    uri_map: dict[str, dict | None] = dict(reports_by_uri or {})
    for row, report in zip(rows, reports, strict=False):
        uri = _row_get(row, "eval_report_gcs_uri")
        if isinstance(uri, str) and uri:
            uri_map.setdefault(uri, report)

    style_net: Counter[str] = Counter()
    style_order: list[str] = []
    tone_net: Counter[str] = Counter()
    tone_order: list[str] = []
    human_rules: Counter[str] = Counter()

    for rating in ratings or ():
        verdict = _row_get(rating, "verdict")
        raw_key = _row_get(rating, "creative_key")
        if (
            verdict not in ("pass", "fail")
            or not isinstance(raw_key, str)
            or not raw_key.strip()
        ):
            continue
        ckey = raw_key.strip()
        if verdict == "fail":
            for tag in classify_human_feedback_tags(_row_get(rating, "note")):
                if tag in ALLOWED_HUMAN_RULES_SET:
                    human_rules[tag] += 1
        r_uri = _row_get(rating, "eval_report_gcs_uri")
        matched_report = (
            uri_map.get(r_uri) if isinstance(r_uri, str) and r_uri else None
        )
        style, tone = _match_rated_style_or_tone(matched_report, ckey)
        weight = _rating_weight(verdict, _row_get(rating, "score"))
        if style:
            style_net[style] += weight
            if style not in style_order:
                style_order.append(style)
        if tone:
            tone_net[tone] += weight
            if tone not in tone_order:
                tone_order.append(tone)

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
        "strongest_styles": _merge_human_ranked(base_styles, style_net, style_order),
        "strongest_tones": _merge_human_ranked(base_tones, tone_net, tone_order),
        "weaknesses": weak.most_common(TOP_N),
        "failed_checks": failed.most_common(TOP_N),
        "human_rules": human_rules.most_common(TOP_N),
    }


def fetch_brand_history(
    brand: str, *, limit: int = 5, bq_client: Any = None, gcs_client: Any = None
) -> dict[str, Any]:
    """Aggregated history of the brand's latest ``limit`` runs; ``{}`` when none.

    Fail-open: any error is logged as a warning and returns ``{}`` (a report
    that can't be read is skipped individually; a failing ratings query degrades
    to eval-only history).
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
        reports_by_uri: dict[str, dict | None] = {}
        for row in rows:
            remaining = REPORT_READ_DEADLINE_SECONDS - (time.monotonic() - started)
            if remaining <= 0.0:
                logger.warning("brand history: report deadline passed; skipping rest")
                reports.extend([None] * (len(rows) - len(reports)))
                break
            uri = _row_get(row, "eval_report_gcs_uri")
            if isinstance(uri, str) and uri in reports_by_uri:
                reports.append(reports_by_uri[uri])
                continue
            if gcs is None and _report_location(uri) is not None:
                gcs = _get_gcs_client()
            gcs_timeout = min(GCS_TIMEOUT_SECONDS, max(1, int(remaining)))
            report = (
                _read_report(gcs, uri, timeout=gcs_timeout) if gcs is not None else None
            )
            if isinstance(uri, str) and uri:
                reports_by_uri[uri] = report
            reports.append(report)

        rating_rows: list[Any] = []
        ratings_table = _ratings_table_id()
        remaining = REPORT_READ_DEADLINE_SECONDS - (time.monotonic() - started)
        if ratings_table is not None and remaining > 0.5:
            try:
                r_sql, r_params = build_ratings_history_query(
                    ratings_table, table, brand, limit=RATINGS_HISTORY_LIMIT
                )
                r_timeout_ms = min(
                    int(BQ_TIMEOUT_SECONDS * 1000),
                    max(1000, int(remaining * 1000)),
                )
                r_cfg = bigquery.QueryJobConfig(
                    query_parameters=r_params,
                    job_timeout_ms=r_timeout_ms,
                )
                for r in bq.query(r_sql, job_config=r_cfg).result():
                    verdict = _row_get(r, "verdict")
                    ckey = _row_get(r, "creative_key")
                    if (
                        verdict not in ("pass", "fail")
                        or not isinstance(ckey, str)
                        or not ckey.strip()
                    ):
                        continue
                    rating_rows.append(r)
                    r_uri = _row_get(r, "eval_report_gcs_uri")
                    r_remaining = REPORT_READ_DEADLINE_SECONDS - (
                        time.monotonic() - started
                    )
                    if (
                        isinstance(r_uri, str)
                        and r_uri
                        and r_uri not in reports_by_uri
                        and r_remaining > 0.0
                    ):
                        if gcs is None and _report_location(r_uri) is not None:
                            gcs = _get_gcs_client()
                        r_gcs_timeout = min(
                            GCS_TIMEOUT_SECONDS, max(1, int(r_remaining))
                        )
                        reports_by_uri[r_uri] = (
                            _read_report(gcs, r_uri, timeout=r_gcs_timeout)
                            if gcs is not None
                            else None
                        )
            except Exception as exc:
                logger.debug("brand history ratings unavailable for %r: %s", brand, exc)
                rating_rows = []

        return aggregate_history(
            brand,
            rows,
            reports,
            ratings=rating_rows,
            reports_by_uri=reports_by_uri,
        )
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
    if guidance := _join(
        f"{rule} ({n})"
        for rule, n in _allowed_counts(
            history.get("human_rules"), ALLOWED_HUMAN_RULES_SET
        )
    ):
        parts.append(f"reviewer guidance: {guidance}")
    head = f"Recent runs for {_clean(history.get('brand') or 'this brand')} ({runs}): "
    tail = (
        " Build on what worked, fix the weaknesses, and favour styles not used "
        "recently."
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
    effect writes its key and ``rating_signals_applied`` records what was used
    (``effects``: only the enabled effects that produced output).
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
        applied = {
            "ratings": n,
            "applied": False,
            "reason": "not_enough_ratings",
            "min": config.rating_learning_min_ratings,
        }
        return {"rating_signals_applied": applied}, [], []
    effects = config.rating_learning_effects & RATING_LEARNING_EFFECTS
    if not effects:  # never "applied" with nothing applied
        applied = {"ratings": n, "applied": False, "reason": "no_effects"}
        return {"rating_signals_applied": applied}, [], []
    # A user style preference wins: the note names no style families then.
    strictness = list(signals["strictness"]) if "checks" in effects else []
    # The note also lists the stricter limits (checks on) so the generators
    # know them up front; without guidance there is no note to carry them.
    note = (
        rating_signals.format_rating_signals(
            signals, brand, include_styles=not keep_shortlist, strictness=strictness
        )
        if "guidance" in effects
        else ""
    )
    steer = "styles" in effects and not keep_shortlist
    excluded = list(signals["styles_excluded"]) if steer else []
    preferred = list(signals["styles_preferred"]) if steer else []
    delta: dict[str, Any] = {}
    if "guidance" in effects:
        delta["rating_signals"] = note
    if "checks" in effects:
        delta["rating_strictness"] = strictness
        delta["rating_limits"] = rating_signals.strictness_limits_rule(strictness)
    # Record only the effects that changed the run, not every configured one.
    produced = {
        "guidance": bool(note),
        "styles": bool(excluded or preferred),
        "checks": bool(strictness),
    }
    delta["rating_signals_applied"] = {
        "ratings": n,
        "applied": True,
        "effects": sorted(e for e in effects if produced[e]),
        "signals": note,
        "strictness": strictness,
        "styles_excluded": excluded,
        "styles_preferred": preferred,
    }
    return delta, excluded, preferred


def shortlist_exclusions(
    recent: Iterable[str], rated_poorly: Iterable[str], rated_well: Iterable[str]
) -> frozenset[str]:
    """The styles the shortlist draw avoids: ``(recent - rated_well) | rated_poorly``.

    Recency (the brand's last runs, for novelty) yields to rating learning: a
    style the team rated well is not excluded for having been used recently,
    so the shortlist matches what ``rating_signals`` tells the art director to
    favour. A rated-poorly style is always excluded (exclusion still beats
    preference in ``pick_style_shortlist``). Case-insensitive.
    """
    liked = {s.strip().lower() for s in rated_well}
    novelty = {s for s in recent if s.strip().lower() not in liked}
    return frozenset(novelty) | frozenset(rated_poorly)


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
    ``visual_style_preference``. A well-rated style beats recency (it is not
    excluded for having been used recently); see ``shortlist_exclusions``.
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
                exclude=shortlist_exclusions(recent, excluded, preferred),
                prefer=preferred,
            )
        )
    return delta
