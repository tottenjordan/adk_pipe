"""Rating signals: what the brand's human ratings say (opt-in rating learning).

When a run opts in (state ``learn_from_ratings``) and ``RATING_LEARNING_ENABLED``
is on, the learning step (``brand_history.brand_history_state_delta``) reads the
brand's recent ``creative_ratings`` rows (all users of the brand, like brand
history) and turns them into three effects:

- **guidance:** a ≤80-word note (``rating_signals``) for the brief writer, ad
  copy drafter and art director;
- **styles:** canonical style families to prefer / exclude when the session's
  ``style_shortlist`` is drawn;
- **checks:** ``rating_strictness``, the recurring, dominant fail reasons that
  map to a tighter deterministic check (read by the gates / image QA).

Trust model: ratings are user-entered and steer other users' runs for the same
brand, so only allowlisted values are ever read or emitted — canonical style
families (``canonical_style``), the ad-copy tone Literal and the fixed
fail-reason enum below. The free-text rating note is never queried (the SELECT
lists its columns explicitly). Minimum sample sizes gate every effect, and
every applied signal is recorded in ``rating_signals_applied``.

Fail-open by design: an unconfigured table or any BigQuery error degrades to
no ratings, never to a failed run.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from google.cloud import bigquery

from agent_common.clients import get_bigquery_client

from .config import config
from .prompt_safe import ALLOWED_TONES, brace_free, normalize_brand
from .style_shortlist import ALL_FAMILIES, canonical_style

logger = logging.getLogger(__name__)

# Shared lazy getter bound to the module's monkeypatch point.
_get_bigquery_client = get_bigquery_client

# Agent-side copy of runserver/rating_reasons.py (agents never import runserver;
# equality is asserted by tests/test_rating_signals.py).
FAIL_REASONS: tuple[str, ...] = (
    "product_not_visible",
    "text_problem",
    "unwanted_logo",
    "weak_cta",
    "off_brief",
    "trend_unclear",
    "cluttered",
    "off_brand_tone",
    "artifacts",
    "other",
)
FAIL_REASON_LABELS: dict[str, str] = {
    "product_not_visible": "Product hard to see",
    "text_problem": "In-image text problems",
    "unwanted_logo": "Unwanted logo / trademark",
    "weak_cta": "Weak call to action",
    "off_brief": "Off-brief / wrong message",
    "trend_unclear": "Trend unclear",
    "cluttered": "Cluttered / weak composition",
    "off_brand_tone": "Off-brand tone",
    "artifacts": "Visual artifacts / quality",
    "other": "Other",
}

# Fail reasons with a deterministic tightening behind them (the only ones that
# can become ``rating_strictness`` flags):
#   product_not_visible → concept guard asks for a large, foreground product;
#                         image QA requires a prominent product
#   text_problem        → concept gate in-image text cap 2 → 1 concept
#   unwanted_logo       → render prompt forbids other logos / trademarks
#   weak_cta            → copy gate CTA ≤ 6 words (from 8)
#   off_brief           → the critic's failed brief checks block for every copy
#   trend_unclear       → image QA requires a clearly visible motif; the concept
#                         guard always appends it
# cluttered / off_brand_tone / artifacts are guidance only; "other" is ignored.
STRICTNESS_REASONS: tuple[str, ...] = (
    "product_not_visible",
    "text_problem",
    "unwanted_logo",
    "weak_cta",
    "off_brief",
    "trend_unclear",
)

VERDICTS = frozenset({"pass", "fail"})
KINDS = frozenset({"visual", "ad_copy"})
MAX_ROWS = 500
BQ_TIMEOUT_SECONDS = 8.0
# A style/tone is preferred at a pass rate ≥ PREFER_RATE and excluded at
# ≤ EXCLUDE_RATE (each needs the minimum sample size first).
PREFER_RATE = 0.67
EXCLUDE_RATE = 0.34
# A fail reason must cover at least this share of the brand's fail ratings to
# become a strictness flag.
DOMINANT_SHARE = 0.30
TOP_N = 3
MAX_WORDS = 80

__all__ = [
    "ALL_FAMILIES",
    "ALLOWED_TONES",
    "FAIL_REASONS",
    "FAIL_REASON_LABELS",
    "STRICTNESS_REASONS",
    "aggregate_ratings",
    "build_ratings_query",
    "fetch_ratings",
    "format_rating_signals",
]


def _table_id() -> str | None:
    parts = [config.BQ_PROJECT_ID, config.BQ_DATASET_ID, config.BQ_TABLE_RATINGS]
    present = [p for p in parts if p]
    return ".".join(present) if len(present) == len(parts) else None


def build_ratings_query(
    table: str, brand: str, days: int
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """The parameterised SELECT for a brand's recent ratings (pure).

    Only allowlisted columns are read — never ``note`` or the judge/user
    fields. Rows store the normalised brand (``normalize_brand``, stamped by
    the api), so the bound ``@brand`` is normalised the same way; ``table``
    comes from config, not user input.
    """
    sql = f"""
        SELECT kind, verdict, visual_style, tone_style, fail_reasons
        FROM `{table}`
        WHERE brand = @brand
          AND updated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
        ORDER BY updated_at DESC
        LIMIT {MAX_ROWS}
    """
    params = [
        bigquery.ScalarQueryParameter("brand", "STRING", normalize_brand(brand)),
        bigquery.ScalarQueryParameter("days", "INT64", int(days)),
    ]
    return sql, params


_COLUMNS = ("kind", "verdict", "visual_style", "tone_style", "fail_reasons")


def _row_dict(row: Any) -> dict[str, Any]:
    """A plain dict of the read columns (a BigQuery ``Row`` has ``.get``)."""
    if isinstance(row, Mapping) or hasattr(row, "get"):
        return {c: row.get(c) for c in _COLUMNS}
    return {c: getattr(row, c, None) for c in _COLUMNS}


def fetch_ratings(brand: str, *, days: int, bq_client: Any = None) -> list[dict]:
    """The brand's ratings of the last ``days`` days (≤ 500, newest first).

    Fail-open: a blank brand or unconfigured table skips the query; any error
    is logged as a warning and returns ``[]``.
    """
    table = _table_id()
    if not normalize_brand(brand) or table is None:
        return []
    try:
        bq = bq_client or _get_bigquery_client()
        sql, params = build_ratings_query(table, brand, days)
        job_config = bigquery.QueryJobConfig(
            query_parameters=params,
            job_timeout_ms=int(BQ_TIMEOUT_SECONDS * 1000),
        )
        return [_row_dict(r) for r in bq.query(sql, job_config=job_config).result()]
    except Exception as exc:
        logger.warning("ratings unavailable for %r: %s", brand, exc)
        return []


def _rated(
    counts: Mapping[str, tuple[int, int]], minimum: int
) -> tuple[list[str], list[str]]:
    """(preferred, excluded) names from ``{name: (n, passes)}`` (pure).

    Preferred best pass rate first, then most ratings; excluded worst first.
    """
    eligible = [(p / n, n, name) for name, (n, p) in counts.items() if n >= minimum]
    preferred = sorted(
        (e for e in eligible if e[0] >= PREFER_RATE), key=lambda e: (-e[0], -e[1])
    )
    excluded = sorted(
        (e for e in eligible if e[0] <= EXCLUDE_RATE), key=lambda e: (e[0], -e[1])
    )
    return [e[2] for e in preferred], [e[2] for e in excluded]


def _tally(counts: dict[str, tuple[int, int]], name: str | None, passed: bool):
    if name:
        n, p = counts.get(name, (0, 0))
        counts[name] = (n + 1, p + int(passed))


def aggregate_ratings(
    rows: Iterable[Mapping[str, Any]], *, style_min: int, reason_min: int
) -> dict[str, Any]:
    """Condense rating rows into allowlisted counts and effects (pure).

    Rows with an unknown kind or verdict are dropped; styles go through
    ``canonical_style``, tones through the tone Literal, reasons through
    ``FAIL_REASONS`` (each counted once per fail rating). ``strictness`` holds
    the check-backed reasons (``STRICTNESS_REASONS``) with ≥ ``reason_min``
    fails that also make up ≥ 30% of the brand's fail ratings.
    """
    ratings = fails = 0
    styles: dict[str, tuple[int, int]] = {}
    tones: dict[str, tuple[int, int]] = {}
    reasons: Counter[str] = Counter()
    for row in rows:
        kind, verdict = row.get("kind"), row.get("verdict")
        if kind not in KINDS or verdict not in VERDICTS:
            continue
        ratings += 1
        passed = verdict == "pass"
        if kind == "visual":
            _tally(styles, canonical_style(row.get("visual_style")), passed)
        else:
            tone = row.get("tone_style")
            _tally(tones, tone if tone in ALLOWED_TONES else None, passed)
        if passed:
            continue
        fails += 1
        raw = row.get("fail_reasons") or []
        if isinstance(raw, list | tuple):
            reasons.update({r for r in raw if r in FAIL_REASON_LABELS})
    styles_preferred, styles_excluded = _rated(styles, style_min)
    tones_preferred, _ = _rated(tones, style_min)
    ranked = sorted(reasons.items(), key=lambda kv: (-kv[1], FAIL_REASONS.index(kv[0])))
    strictness = [
        r
        for r, n in ranked
        if r in STRICTNESS_REASONS and n >= reason_min and n >= DOMINANT_SHARE * fails
    ]
    return {
        "ratings": ratings,
        "fails": fails,
        "styles_preferred": styles_preferred,
        "styles_excluded": styles_excluded,
        "tones_preferred": tones_preferred,
        "fail_reasons": dict(ranked),
        "reason_min": reason_min,
        "strictness": strictness,
    }


def _styles(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for v in values:
        family = canonical_style(v) if isinstance(v, str) else None
        if family and family == v and family not in out:
            out.append(family)
    return out


def _join(items: Iterable[str]) -> str:
    return ", ".join(items)


def format_rating_signals(signals: Mapping[str, Any], brand: str) -> str:
    """The ratings as one short, brace-free note; ``""`` without ratings.

    Only canonical style families, allowlisted tones and the fail-reason labels
    are rendered (a handcrafted dict is filtered the same way); ``other`` is
    never named. Fail reasons need ``reason_min`` fails (default 1 when absent).
    """
    ratings = int(signals.get("ratings") or 0) if signals else 0
    if ratings <= 0:
        return ""
    preferred = _styles(signals.get("styles_preferred") or [])[:TOP_N]
    excluded = _styles(signals.get("styles_excluded") or [])[:TOP_N]
    tones = [t for t in signals.get("tones_preferred") or [] if t in ALLOWED_TONES]
    reason_min = int(signals.get("reason_min") or 1)
    raw_reasons = signals.get("fail_reasons") or {}
    reasons = [
        f"{FAIL_REASON_LABELS[r].lower()} ({int(n)})"
        for r, n in (raw_reasons.items() if isinstance(raw_reasons, Mapping) else [])
        if r in FAIL_REASON_LABELS and r != "other" and int(n) >= reason_min
    ][:TOP_N]
    parts: list[str] = []
    well = [
        f"{joined} {label}"
        for joined, label in (
            (_join(preferred), "visuals"),
            (_join(tones[:TOP_N]), "copy"),
        )
        if joined
    ]
    if well:
        parts.append("rated well: " + "; ".join(well))
    if excluded:
        parts.append(f"rated poorly: {_join(excluded)} visuals")
    if reasons:
        parts.append(f"common fail reasons: {_join(reasons)}")
    if not parts:
        parts.append("no clear pattern yet")
    head = f"Your team's ratings for {brace_free(brand) or 'this brand'} ({ratings}): "
    tail = " Favour what was rated well and avoid these failure causes."
    body = "; ".join(parts) + "."
    budget = MAX_WORDS - len(head.split()) - len(tail.split())
    words = body.split()
    if len(words) > budget:
        body = " ".join(words[: budget - 1]).rstrip(",;.") + " …"
    return head + body + tail
