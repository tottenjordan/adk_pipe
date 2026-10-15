"""Opt-in safer angle for sensitive trends (plan
docs/plans/2026-10-15-safer-angle-sensitive-trends.md, PR A1).

Pure logic (no ADK imports). When a user deliberately picks a trend that
trend_scout flagged sensitive (``trend_scout_context`` JSON with
``"sensitive": true``) AND opts in (``safer_angle_opt_in`` exactly ``True``),
research, the brief, ad copy and visuals look for a respectful, positive angle
on the trend's subject (e.g. an artist's legacy) and never reference the
sensitive event itself.

``derive_state`` turns the seeded inputs into the run's state contract:

* ``safer_angle_opt_in`` — bool, only exactly ``True`` counts;
* ``trend_sensitive_hint`` — ``"yes"`` / ``""``: the scout flagged the trend;
* ``safer_angle_active`` — ``"yes"`` / ``""`` = opt-in AND hint;
* ``safer_angle_sensitive_reason`` — the hint's ``sensitive_reason`` (capped);
* ``safer_angle_research_rules`` / ``_brief_rules`` / ``_copy_rules`` /
  ``_visual_rules`` — brace-free rule texts read as optional ``{…?}`` tokens at
  the end of the prompts, ``""`` when inactive (so runs without the opt-in get
  byte-identical instructions).

The brief's angle itself lives in ``CreativeBrief.trend_bridge.safer_angle``,
checked by ``brief_check.check_brief(safer_angle_active=)``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

MAX_REASON_CHARS = 120
ACTIVE = "yes"

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_BRACES = re.compile(r"[{}]")
_SPACE = re.compile(r"\s+")


def _clean_reason(value: Any) -> str:
    """``value`` as a one-line, brace-free reason of at most MAX_REASON_CHARS."""
    if not isinstance(value, str):
        return ""
    text = _SPACE.sub(" ", _BRACES.sub("", value)).strip()
    if len(text) > MAX_REASON_CHARS:
        text = text[:MAX_REASON_CHARS].rstrip()
    return text


def parse_hint(raw: Any) -> tuple[bool, str]:
    """``(sensitive, reason)`` from a ``trend_scout_context`` value.

    Lenient: a JSON string (optionally fenced) or an already-parsed dict. Only
    a JSON object whose ``sensitive`` is exactly ``True`` counts; anything else
    (empty, malformed, a non-object, ``"true"`` as a string) is
    ``(False, "")``. The reason is cleaned (one line, no braces, capped)."""
    data: Any = raw
    if isinstance(raw, str):
        text = _FENCE.sub("", raw.strip())
        if not text:
            return False, ""
        try:
            data = json.loads(text)
        except ValueError:
            return False, ""
    if not isinstance(data, Mapping) or data.get("sensitive") is not True:
        return False, ""
    return True, _clean_reason(data.get("sensitive_reason"))


# --- rule texts (brace-free: they are state VALUES spliced into prompts) --- #
def _reason_phrase(reason: str) -> str:
    cleaned = _clean_reason(reason)
    return cleaned if cleaned else "flagged sensitive by the trend scout"


_FLOOR_EVENT = (
    "never reference the sensitive event, its victims, injuries, damage, trial, "
    "verdict, sentence or cause of death"
)
_FLOOR_JOKES = (
    'no jokes, puns or wordplay about the harm, and no "RIP" or "rest in peace" '
    "sales hooks"
)
_FLOOR_ENDORSEMENT = (
    "never imply endorsement, partnership, licensing, sponsorship or approval by "
    'the person or their estate (no "official", "signature model", "as used by", '
    '"his/her favourite"), and never imply the person used or owned the product'
)
_FLOOR_WORDS = "never put words in their mouth: no quotes or lyrics"
_FLOOR_URGENCY = "no urgency or discount pushes tied to the moment"
_FLOOR_LIKENESS = (
    "no likeness of the real person and no estate-owned imagery, logos or album art"
)


def _floor(*, likeness: bool) -> str:
    parts = [_FLOOR_EVENT, _FLOOR_JOKES, _FLOOR_ENDORSEMENT, _FLOOR_WORDS]
    if likeness:
        parts.append(_FLOOR_LIKENESS)
    parts.append(_FLOOR_URGENCY)
    return "\n    *   Safety floor: " + "; ".join(parts) + "."


def research_rules(reason: str) -> str:
    """Rules for the trend researcher's planner, searcher and synthesizer."""
    return (
        "\n\n    **Safer angle (this run):** this trend is sensitive ("
        + _reason_phrase(reason)
        + ") and the user asked for a respectful angle on it."
        "\n    *   Research the trend subject's positive legacy, cultural "
        "contributions and the values audiences admire (e.g. an artist's music, "
        "message and influence), not the sensitive event."
        "\n    *   When the subject is a real person, name the values and legacy "
        "they are celebrated for: what fans admire them for and why."
        "\n    *   Never research or describe the event's details beyond naming "
        "it as a risk."
        '\n    *   The trend report ends with a "## Safer Angle Candidates" '
        "section: 2-3 positive associations, each with why it is respectful and "
        "a source."
    )


def brief_rules(reason: str) -> str:
    """Rules for the brief writer (and reviser, which shares its instruction)."""
    return (
        "\n\n    **Safer angle (this run):** the user deliberately picked this "
        "sensitive trend ("
        + _reason_phrase(reason)
        + ") and asked for a respectful, positive angle on its subject."
        "\n    *   For this run this replaces rule 13's \"a current moment marked "
        "sensitive is never referenced\" for the trend's subject, never for the "
        "event: set trend_bridge.safer_angle to ONE positive association from the "
        "research (at most 25 words) and build the angles on it."
        "\n    *   When the trend's subject is a real person (e.g. an artist), the "
        "safer angle names them and the positive thing they are celebrated for "
        "(e.g. an artist's legacy of chasing your music dreams), and fit_mode may "
        'be "cultural" (score 3): for this run rule 13\'s light_touch applies to '
        "the sensitive event only, never to the person's legacy. When the subject "
        "is an event with no person to honour (a disaster, a crime), keep a light "
        "touch."
        "\n    *   The subject may be named only as a respectful tribute or "
        "inspiration; the event is never referenced."
        "\n    *   List the event's harms in trend_bridge.risks and short terms for "
        "them in avoid; the safer angle shares no word with them."
        + _floor(likeness=True)
        + "\n    *   Leave safer_angle empty only if no association can respect "
        "this floor."
    )


def copy_rules(reason: str) -> str:
    """Rules for the ad copy drafter, critic and reviser (read by PR A2)."""
    return (
        "\n\n    **Safer angle (this run):** the trend is sensitive ("
        + _reason_phrase(reason)
        + "); the brief's Safer angle is the respectful, positive association to "
        "build on."
        "\n    *   Use the safer angle to tie at least half of the copies clearly "
        "to the trend; name the subject only in a respectful tribute or "
        "inspiration framing."
        "\n    *   When the subject is a real person, 1-2 of the copies name them "
        "once in a respectful tribute or inspiration framing (their drive, their "
        "legacy, what they inspire), in your own words; the rest stay tied to the "
        "safer angle." + _floor(likeness=False)
    )


def visual_rules(reason: str) -> str:
    """Rules for the art director and visual producers (read by PR A2)."""
    return (
        "\n\n    **Safer angle (this run):** the trend is sensitive ("
        + _reason_phrase(reason)
        + "); the brief's Safer angle is the respectful, positive association to "
        "build on."
        "\n    *   Draw motifs from the safer angle (era, genre, instruments, "
        "settings); a real person may be evoked through their era, genre, "
        "instruments and settings, never a likeness of the real person and never "
        "estate-owned imagery."
        "\n    *   Signature objects or attire of the real person (e.g. a bandana) "
        "may appear as props or styling cues, on the set, the instrument or an "
        "anonymous figure who does not resemble them; never a likeness."
        + _floor(likeness=True)
    )


RULE_KEYS = (
    "safer_angle_research_rules",
    "safer_angle_brief_rules",
    "safer_angle_copy_rules",
    "safer_angle_visual_rules",
)


def derive_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """The run's safer-angle state contract from the seeded inputs (pure)."""
    opt_in = state.get("safer_angle_opt_in") is True
    sensitive, reason = parse_hint(state.get("trend_scout_context"))
    active = opt_in and sensitive
    builders = (research_rules, brief_rules, copy_rules, visual_rules)
    return {
        "safer_angle_opt_in": opt_in,
        "trend_sensitive_hint": ACTIVE if sensitive else "",
        "safer_angle_active": ACTIVE if active else "",
        "safer_angle_sensitive_reason": reason if sensitive else "",
        **{
            key: build(reason) if active else ""
            for key, build in zip(RULE_KEYS, builders, strict=True)
        },
    }


def is_active(state: Mapping[str, Any]) -> bool:
    """Is the run's safer angle active (``safer_angle_active == "yes"``)?"""
    return state.get("safer_angle_active") == ACTIVE


def is_flagged_sensitive(state: Mapping[str, Any]) -> bool:
    """Did trend_scout flag the trend sensitive (``trend_sensitive_hint ==
    "yes"``, derived by ``derive_state``)?"""
    return state.get("trend_sensitive_hint") == ACTIVE


def clear_stray_safer_angle(brief: Mapping[str, Any]) -> dict[str, Any] | None:
    """``brief`` with a non-empty ``trend_bridge.safer_angle`` emptied, or None
    when there is nothing to clear. For runs without an active safer angle, so
    the creative prompts never see one."""
    bridge = brief.get("trend_bridge")
    if not isinstance(bridge, Mapping):
        return None
    angle = bridge.get("safer_angle")
    if not (isinstance(angle, str) and angle.strip()):
        return None
    return {**brief, "trend_bridge": {**bridge, "safer_angle": ""}}
