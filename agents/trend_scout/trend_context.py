"""Compact per-trend research context for `target_trends_crf.trend_context`.

trend_scout's research (`info_gtrends`, the synthesizer's JSON, and
`selected_gtrends`, the picker's Markdown) used to be dropped at the BigQuery
hand-off, so a batch creative run only ever saw the trend term. This module
pulls the few load-bearing fields for ONE term out of both and returns compact
JSON (`{context, cultural_angle, event_hint, hook}`, blank fields dropped, plus
`sensitive` / `sensitive_reason` only for a term the classifier flagged
sensitive in `trend_flags`) — or "" when nothing parses. Pure and best-effort:
it never raises, so a malformed model output can never fail the BigQuery write.

The creative run treats the forwarded value as a hint to re-verify, never as a
fact (creative_agent prompts read it as `{trend_scout_context?}`).
"""

import json
import re
from typing import Any

MAX_FIELD_CHARS = 400

# A leading/trailing Markdown code fence around model JSON (```json ... ```).
FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_HEADING = re.compile(r"^\s{0,3}(#{2,3})\s+(.*?)\s*#*\s*$")
_HOOK = re.compile(r"\*\*\s*The\s+\"?Hook\"?\s*:?\s*\*\*\s*:?\s*(.*)", re.IGNORECASE)
_CONTEXT = re.compile(r"\*\*\s*Context\s*:?\s*\*\*\s*:?\s*(.*)", re.IGNORECASE)


def norm_term(term: Any) -> str:
    """Case/space/quote-insensitive key for matching a term to its research."""
    if not isinstance(term, str):
        return ""
    return " ".join(term.strip().strip("\"'`*[]").split()).casefold()


def _clean(value: Any) -> str:
    """A single-line, capped string ('' for non-strings)."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:MAX_FIELD_CHARS].rstrip()


def _info_item(term_key: str, info_gtrends: Any) -> dict[str, Any]:
    """The `analyzed_trends` entry for the term from the synthesizer JSON, or {}."""
    data = info_gtrends
    if isinstance(data, str):
        try:
            data = json.loads(FENCE.sub("", data))
        except ValueError:
            return {}
    if not isinstance(data, dict):
        return {}
    items = data.get("analyzed_trends")
    if not isinstance(items, list):
        return {}
    for item in items:
        if isinstance(item, dict) and norm_term(item.get("term")) == term_key:
            return item
    return {}


def is_true(value: Any) -> bool:
    """True for ``True`` or the string "true" (any case/spacing), else False."""
    return value is True or (
        isinstance(value, str) and value.strip().casefold() == "true"
    )


def sensitive_flags(trend_flags: Any) -> dict[str, str]:
    """``{normalized term: reason}`` for the terms flagged sensitive, or {}.

    Reads state ``trend_flags`` (the classifier's ``{"flags": [{term, sensitive,
    reason}]}``) as a dict, a JSON string or fenced JSON. Best-effort: malformed
    input gives {} (nothing flagged), never an exception.
    """
    data = trend_flags
    if isinstance(data, str):
        try:
            data = json.loads(FENCE.sub("", data))
        except ValueError:
            return {}
    if isinstance(data, dict):
        data = data.get("flags")
    if not isinstance(data, list):
        return {}
    flagged: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict) or not is_true(item.get("sensitive")):
            continue
        key = norm_term(item.get("term"))
        if key and key not in flagged:
            flagged[key] = _clean(item.get("reason"))
    return flagged


def _selected_section(term_key: str, selected_gtrends: Any) -> dict[str, str]:
    """Hook + Context from the term's `### term` section of the picker Markdown."""
    if not isinstance(selected_gtrends, str):
        return {}
    in_section = False
    found: dict[str, str] = {}
    for line in selected_gtrends.splitlines():
        heading = _HEADING.match(line)
        if heading:
            if in_section:
                break
            in_section = (
                heading.group(1) == "###" and norm_term(heading.group(2)) == term_key
            )
            continue
        if not in_section:
            continue
        for key, pattern in (("hook", _HOOK), ("context", _CONTEXT)):
            match = pattern.search(line)
            if match and key not in found:
                found[key] = match.group(1)
    return found


def trend_context_for(
    term: str, info_gtrends: Any, selected_gtrends: Any, flags: Any = None
) -> str:
    """Compact JSON context for ``term`` (blank fields dropped), or "".

    Args:
        term: the trend term written to the row.
        info_gtrends: the synthesizer output (JSON string, fenced JSON, or dict).
        selected_gtrends: the picker's Markdown (`### term` sections).
        flags: state ``trend_flags`` (dict, JSON string or fenced JSON). Only a
            term flagged sensitive gains ``"sensitive": true`` and
            ``"sensitive_reason"`` (after the other keys); an unflagged term's
            JSON is unchanged.
    """
    term_key = norm_term(term)
    if not term_key:
        return ""
    item = _info_item(term_key, info_gtrends)
    section = _selected_section(term_key, selected_gtrends)
    fields = {
        "context": _clean(item.get("context")) or _clean(section.get("context")),
        "cultural_angle": _clean(item.get("cultural_angle")),
        "event_hint": _clean(item.get("event_hint")),
        "hook": _clean(section.get("hook")),
    }
    compact: dict[str, Any] = {key: value for key, value in fields.items() if value}
    flagged = sensitive_flags(flags)
    if term_key in flagged:
        compact["sensitive"] = True
        compact["sensitive_reason"] = flagged[term_key]
    if not compact:
        return ""
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
