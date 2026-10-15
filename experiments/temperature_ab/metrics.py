"""Per-run metrics for the temperature A/B (pure, offline), plus the log scrape.

State metrics read the final session state each ``/runs`` poll already returns,
so they cost no extra quota. The log scrape counts robustness signals the state
doesn't keep (empty/abnormal model turns, schema ``ValidationError``s, 429s) in
the run's Cloud Logging window on its arm's revision; the reader is injectable
so tests never call gcloud.
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
from collections.abc import Callable, Iterable

from experiments.creative_latency.run_trial import _build_log_filter
from experiments.quota_spread.quality import extract_quality

ISSUES_SUFFIX = "__issues"
RETRY_EXHAUSTED_SUFFIX = "__retry_exhausted"
SELECTED_TRENDS = 3
# `### term` headings of the picker's Markdown (trend_scout/trend_context.py
# reads the same sections).
_SELECTED_HEADING = re.compile(r"^\s{0,3}###\s+\S")
_WORD = re.compile(r"[a-z0-9']+")


def count_issues(state: dict) -> int:
    """Sum of residual-quality issues over every ``*__issues`` key.

    A list counts its length, a non-empty string counts 1, ``None``/empty 0.
    """
    total = 0
    for key, value in state.items():
        if not key.endswith(ISSUES_SUFFIX):
            continue
        if isinstance(value, list | tuple):
            total += len(value)
        elif isinstance(value, str):
            total += 1 if value.strip() else 0
        elif value:
            total += 1
    return total


def count_retry_exhausted(state: dict) -> int:
    """Number of truthy ``*__retry_exhausted`` markers (``None`` = cleared)."""
    return sum(
        1 for k, v in state.items() if k.endswith(RETRY_EXHAUSTED_SUFFIX) and bool(v)
    )


def final_headlines(state: dict) -> list[str]:
    """Headlines of the final ad copies (``ad_copy_critique.ad_copies[*].headline``,
    the ``FinalAdCopyList`` output of the critic / reviser)."""
    critique = state.get("ad_copy_critique")
    if not isinstance(critique, dict):
        return []
    copies = critique.get("ad_copies")
    if not isinstance(copies, list):
        return []
    return [
        c["headline"]
        for c in copies
        if isinstance(c, dict) and isinstance(c.get("headline"), str)
    ]


def _words(text: str) -> frozenset[str]:
    return frozenset(_WORD.findall(text.lower()))


def headline_jaccard(headlines: Iterable[str]) -> float | None:
    """Mean pairwise Jaccard similarity of the headlines' lower-cased word sets.

    Lower = more diverse. ``None`` with fewer than two headlines.
    """
    sets = [_words(h) for h in headlines]
    pairs = list(itertools.combinations(sets, 2))
    if not pairs:
        return None
    scores = [len(a & b) / len(a | b) if a | b else 1.0 for a, b in pairs]
    return sum(scores) / len(scores)


def image_qa_attempts(state: dict) -> int:
    """Total render attempts over ``generated_images[*].attempts`` (missing = 0)."""
    images = state.get("generated_images")
    if not isinstance(images, dict):
        return 0
    total = 0
    for record in images.values():
        if isinstance(record, dict) and isinstance(record.get("attempts"), int):
            total += record["attempts"]
    return total


def creative_metrics(state: dict) -> dict:
    """creative_agent metrics from a run's final session state."""
    state = state if isinstance(state, dict) else {}
    report = state.get("creative_evaluation_report")
    summary = report.get("summary") if isinstance(report, dict) else None
    gates = summary.get("gates_pass_rate") if isinstance(summary, dict) else None
    quality = extract_quality(state) or {}
    return {
        "finalized": bool(state.get("finalize_done")),
        "gates_pass_rate": gates,
        "mean_score": quality.get("mean_score"),
        "pass_rate": quality.get("pass_rate"),
        "n_issues": count_issues(state),
        "n_retry_exhausted": count_retry_exhausted(state),
        "image_qa_attempts": image_qa_attempts(state),
        "headline_jaccard": headline_jaccard(final_headlines(state)),
    }


def trend_flags_map(state: dict) -> dict[str, bool]:
    """``trend_flags`` (``TrendFlags``: ``{flags: [{term, sensitive, reason}]}``)
    as a term → sensitive map."""
    raw = state.get("trend_flags")
    flags = raw.get("flags") if isinstance(raw, dict) else None
    out: dict[str, bool] = {}
    for item in flags if isinstance(flags, list) else []:
        if isinstance(item, dict) and isinstance(item.get("term"), str):
            out[item["term"]] = bool(item.get("sensitive"))
    return out


def n_selected_trends(state: dict) -> int:
    """``### term`` sections in ``selected_gtrends`` (the picker's Markdown)."""
    text = state.get("selected_gtrends")
    if not isinstance(text, str):
        return 0
    return sum(1 for line in text.splitlines() if _SELECTED_HEADING.match(line))


def trend_scout_metrics(state: dict) -> dict:
    """trend_scout metrics from a run's final session state."""
    state = state if isinstance(state, dict) else {}
    n_selected = n_selected_trends(state)
    return {
        "completed": n_selected == SELECTED_TRENDS,
        "n_selected": n_selected,
        "trend_flags": trend_flags_map(state),
        "info_gtrends_exhausted": bool(state.get("info_gtrends__retry_exhausted")),
        "n_retry_exhausted": count_retry_exhausted(state),
    }


def state_metrics(app: str, state: dict) -> dict:
    if app == "creative_agent":
        return creative_metrics(state)
    if app == "trend_scout":
        return trend_scout_metrics(state)
    raise ValueError(f"unsupported app {app!r}")


# --- Cloud Logging scrape ----------------------------------------------------

# Exact log texts: agent_common/observability.py `log_empty_turn_finish_reason`
# and agent_common/models.py's empty-turn re-ask warning.
ABNORMAL_TURN_TEXT = "empty/abnormal model turn"
EMPTY_TURN_TEXT = "returned an empty turn"
VALIDATION_ERROR_TEXT = "ValidationError"
_429 = re.compile(r"\b429\b|RESOURCE_EXHAUSTED")


def _either(pattern: str) -> str:
    return f'textPayload=~"{pattern}" OR jsonPayload.message=~"{pattern}"'


SIGNALS = " OR ".join(
    [
        _either(ABNORMAL_TURN_TEXT),
        _either(EMPTY_TURN_TEXT),
        _either(VALIDATION_ERROR_TEXT),
        "httpRequest.status=429",
        _either("429"),
        _either("RESOURCE_EXHAUSTED"),
    ]
)
LOG_COUNT_KEYS = ("abnormal_turns", "empty_turns", "validation_errors", "http_429")


def build_signal_filter(*, revision: str, start_epoch: float, end_epoch: float) -> str:
    """Cloud Logging filter for this experiment's signals in one run's window."""
    return _build_log_filter(
        revision=revision,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        signals=SIGNALS,
    )


def _entry_text(entry: dict) -> str:
    text = entry.get("textPayload")
    if isinstance(text, str):
        return text
    payload = entry.get("jsonPayload")
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    return ""


def classify_log_entries(entries: Iterable[dict]) -> dict[str, int]:
    """Count each signal; one entry may count toward several signals."""
    counts = dict.fromkeys(LOG_COUNT_KEYS, 0)
    for entry in entries:
        text = _entry_text(entry)
        if ABNORMAL_TURN_TEXT in text:
            counts["abnormal_turns"] += 1
        if EMPTY_TURN_TEXT in text:
            counts["empty_turns"] += 1
        if VALIDATION_ERROR_TEXT in text:
            counts["validation_errors"] += 1
        http = entry.get("httpRequest")
        status = http.get("status") if isinstance(http, dict) else None
        if status == 429 or _429.search(text):
            counts["http_429"] += 1
    return counts


def gcloud_log_reader(log_filter: str) -> list[dict]:
    """Live reader: ``gcloud logging read`` (the only GCP call in this module)."""
    out = subprocess.run(
        [
            "gcloud",
            "logging",
            "read",
            log_filter,
            "--format=json",
            "--limit=5000",
            "--freshness=1d",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    )
    return json.loads(out.stdout or "[]")


def scrape_log_counts(
    revision: str,
    start_epoch: float,
    end_epoch: float,
    *,
    reader: Callable[[str], list[dict]] = gcloud_log_reader,
) -> dict[str, int] | None:
    """Signal counts for one run, or ``None`` when unscoped or the read fails.

    Best effort, like ``count_429s``: log access never sinks a run record.
    """
    log_filter = build_signal_filter(
        revision=revision, start_epoch=start_epoch, end_epoch=end_epoch
    )
    if not log_filter:
        return None
    try:
        return classify_log_entries(reader(log_filter))
    except Exception:  # noqa: BLE001 — enrichment only; degrade to None
        return None
