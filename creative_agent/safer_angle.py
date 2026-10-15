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
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .text_match import contains_phrase, content_tokens, same_word, words
from .trend_tie import distinctive_trend_tokens, has_trend_tie, tie_terms

if TYPE_CHECKING:  # run_policy imports this module
    from .run_policy import RunPolicy

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


# --- the copy gate's safer-angle tie (called by copy_gate.gate_copies) --- #
# Words a safer angle shares with any music / craft / feel-good tribute: they
# never make a copy's tie specific ("Own the Spark of Golden-Era Soul" is not
# tied to "Tupac Shakur's ... soulful, analog groove"), so only the angle's
# other content words (plus the subject's name) count. Pronouns and filler
# are here too (text_match.STOPWORDS are already out).
GENERIC_SAFER_ANGLE_WORDS = frozenset(
    {
        "legacy", "enduring", "lasting", "artistic", "art", "artist", "artistry",
        "soul", "soulful", "heart", "heartfelt", "spirit", "passion",
        "music", "musical", "musician", "sound", "sounds", "song", "songs",
        "studio", "analog", "analogue", "organic", "groove", "grooves",
        "craft", "crafted", "craftsmanship", "handcrafted", "expression",
        "expressive", "creative", "creativity", "inspiration", "inspired",
        "inspiring", "tribute", "honor", "honour", "celebrated", "celebrating",
        "celebration", "iconic", "icon", "legend", "legendary", "timeless",
        "classic", "golden", "era", "culture", "cultural", "style", "story",
        "vibe", "vibes", "energy", "spark", "magic", "rhythm", "driven",
        "voice", "love", "life", "dream", "dreams", "chasing", "community",
        "positive", "influence", "influential", "impact", "work", "works",
        "known", "famous", "beloved", "generation", "generations",
        "he", "she", "him", "his", "her", "hers", "they", "them", "you",
        "your", "we", "our", "who", "that", "was", "were", "be", "been",
        "has", "have", "had", "not", "no", "all", "after", "before",
    }
)  # fmt: skip
SAFER_ANGLE_MIN_WORD_CHARS = 4
_SHORT_ANGLE_WORDS = 8
_MAX_SAFER_EXAMPLES = 3


def safer_angle_tie_avoid(
    trend: str,
    *,
    avoid: Sequence[str] = (),
    risks: Sequence[str] = (),
    safer_angle: str = "",
) -> list[str]:
    """``avoid`` plus the trend's distinctive tokens that a brief risk names
    but the safer angle does not: the sensitive event's words ("murder",
    "trial" in "tupac shakur murder trial") never count as a trend tie, while
    the subject's name, which the safer angle uses, still does."""
    extra = [
        token
        for token in distinctive_trend_tokens(trend, avoid)
        if any(contains_phrase(risk, token) for risk in risks)
        and not contains_phrase(safer_angle, token)
    ]
    return [*avoid, *extra]


def named_subject_keys(
    texts: Sequence[tuple[str, str]],
    *,
    trend_terms: str,
    avoid: Sequence[str] = (),
    risks: Sequence[str] = (),
    safer_angle: str = "",
) -> list[str]:
    """The keys of the ``(key, text)`` pairs whose text names the trend's
    subject: the trend phrase or one of its distinctive words, minus the
    ``safer_angle_tie_avoid`` words (motifs do not count). [] without a
    trend."""
    if not words(trend_terms):
        return []
    tie_avoid = safer_angle_tie_avoid(
        trend_terms, avoid=avoid, risks=risks, safer_angle=safer_angle
    )
    return [
        key
        for key, text in texts
        if has_trend_tie(text, trend=trend_terms, avoid=tie_avoid)
    ]


def safer_angle_tie_words(
    safer_angle: str,
    *,
    trend_terms: str = "",
    target_product: str = "",
    brand: str = "",
    avoid: Sequence[str] = (),
    risks: Sequence[str] = (),
) -> list[str]:
    """The safer angle's distinctive content words (folded, first occurrence
    order): its ``content_tokens`` minus ``GENERIC_SAFER_ANGLE_WORDS``, words
    under ``SAFER_ANGLE_MIN_WORD_CHARS`` characters, the trend's own words
    (naming the subject is checked separately), the product / brand words and
    the words of ``avoid`` / ``risks`` (singular/plural alike)."""
    if not isinstance(safer_angle, str):
        return []
    excluded = [
        w
        for phrase in (trend_terms, target_product, brand, *avoid, *risks)
        if isinstance(phrase, str)
        for w in words(phrase)
    ]
    out: list[str] = []
    for word in content_tokens(safer_angle):
        if (
            word in GENERIC_SAFER_ANGLE_WORDS
            or len(word) < SAFER_ANGLE_MIN_WORD_CHARS
            or word in out
            or any(same_word(word, other) for other in excluded)
        ):
            continue
        out.append(word)
    return out


def _short_angle(safer_angle: str) -> str:
    parts = safer_angle.split()
    if len(parts) <= _SHORT_ANGLE_WORDS:
        return safer_angle.strip()
    return " ".join(parts[:_SHORT_ANGLE_WORDS]).rstrip(",;:.") + "…"


def untied_copy_issues(
    texts: Sequence[tuple[str, str]], policy: RunPolicy, avoid: Sequence[str]
) -> dict[str, str]:
    """The safer-angle tie issue per untied copy key, when fewer than
    ceil(n/2) of the ``(key, text)`` copies tie (name the subject, as
    ``named_subject_keys``, or use a ``safer_angle_tie_words`` word); {}
    otherwise or without a trend. ``avoid`` is the copy gate's filtered avoid
    terms; the trend, product, brand, risks and angle come from ``policy``."""
    trend_terms = policy.trend
    risks = policy.risks
    angle_text = policy.safer_angle_text
    if not texts or not words(trend_terms):
        return {}
    named = set(
        named_subject_keys(
            texts,
            trend_terms=trend_terms,
            avoid=avoid,
            risks=risks,
            safer_angle=angle_text,
        )
    )
    angle_words = safer_angle_tie_words(
        angle_text,
        trend_terms=trend_terms,
        target_product=policy.target_product,
        brand=policy.brand,
        avoid=avoid,
        risks=risks,
    )
    untied: list[str] = []
    for key, text in texts:
        text_words = words(text)
        if key not in named and not any(
            same_word(a, w) for a in angle_words for w in text_words
        ):
            untied.append(key)
    need = -(-len(texts) // 2)
    if len(texts) - len(untied) >= need:
        return {}
    tie_avoid = safer_angle_tie_avoid(
        trend_terms, avoid=avoid, risks=risks, safer_angle=angle_text
    )
    examples = ", ".join(
        [
            *tie_terms(trend_terms, avoid=tie_avoid)[:1],
            *(w.title() for w in angle_words),
        ][:_MAX_SAFER_EXAMPLES]
    )
    angle = f" ({_short_angle(angle_text)})" if angle_text.strip() else ""
    issue = (
        f"not tied to the safer angle{angle}: name the subject in tribute or use "
        "a distinctive part of its legacy, not just generic words"
        + (f" (e.g. {examples})" if examples else "")
        + f"; at least {need} of the {len(texts)} copies must."
    )
    return dict.fromkeys(untied, issue)
