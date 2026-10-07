"""Deterministic checks on the structured CreativeBrief.

Pure logic (no ADK imports) behind ``creative_agent.agent.brief_gate``: the
rules the ``CreativeBrief`` output schema cannot express. Each issue string is
specific and actionable because the gate feeds them verbatim to
``brief_reviser`` (as ``brief_issues``) and records any that survive the bounded
revision as ``creative_brief__issues`` (surfaced by
``agent_common.observability.collect_degradation_warnings``).
"""

import json
import re
from collections.abc import Mapping
from typing import Any

MISSING_BRIEF_ISSUE = "brief missing or unparseable"

# Imagery that could illustrate any trend: a motif made only of these words
# does not tie the work to THIS trend (mirrors the #258 visual motif rule).
GENERIC_MOTIFS = frozenset(
    {
        "phone",
        "smartphone",
        "social feed",
        "feed",
        "chat bubble",
        "hashtag",
        "emoji",
        "laptop",
        "screen",
        "notification",
        "social media",
    }
)
_GENERIC_TOKENS = frozenset(w for term in GENERIC_MOTIFS for w in term.split()) | {
    "post",
    "glowing",
}
_FILLER = frozenset({"a", "an", "the", "of", "on", "with", "and"})

_TENSION = re.compile(r"\b(but|yet|although|even though|while)\b", re.IGNORECASE)
_AND = re.compile(r"\band\b", re.IGNORECASE)
# A sentence break: terminal punctuation followed by whitespace and more text
# (so "2.5x" or a single trailing period is one sentence).
_SENTENCE_BREAK = re.compile(r"[.!?]+\s+\S")


def _expected_fit_mode(score: int) -> str:
    if score >= 4:
        return "direct"
    if score == 3:
        return "cultural"
    return "light_touch"


def _parse(brief: Mapping[str, Any] | str | None) -> Mapping[str, Any] | None:
    if isinstance(brief, str):
        try:
            brief = json.loads(brief)
        except ValueError:
            return None
    return brief if isinstance(brief, Mapping) and brief else None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_generic_motif(motif: str) -> bool:
    words = re.findall(r"[a-z]+", motif.lower())
    words = [w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words]
    words = [w for w in words if w not in _FILLER]
    return not words or all(w in _GENERIC_TOKENS for w in words)


def check_brief(
    brief: Mapping[str, Any] | str | None, *, brand_colors: str = ""
) -> list[str]:
    """Return the brief's rule violations as actionable issue strings ([] = clean).

    ``brief`` is the ``creative_brief`` state value: a dict (ADK stores parsed
    ``output_schema`` output) or its JSON string. ``brand_colors`` is the user's
    optional palette; when given, the brief must name distinctive assets.
    Never raises: malformed fields are reported as issues.
    """
    data = _parse(brief)
    if data is None:
        return [MISSING_BRIEF_ISSUE]

    issues: list[str] = []

    proposition = _text(data.get("single_minded_proposition"))
    if not proposition:
        issues.append(
            "single_minded_proposition is empty: write ONE sentence carrying the "
            "single idea the audience should take away."
        )
    elif _SENTENCE_BREAK.search(proposition):
        issues.append(
            f"single_minded_proposition must be one sentence; rewrite "
            f"'{proposition}' as a single sentence with a single idea."
        )
    elif _AND.search(proposition):
        issues.append(
            f"single_minded_proposition joins ideas with 'and' ('{proposition}'); "
            "keep only the strongest single idea."
        )

    insight = _text(data.get("insight"))
    if not _TENSION.search(insight):
        issues.append(
            f"insight has no tension ('{insight}'); rewrite it as 'X, but Y' "
            "(using but/yet/although/even though/while) specific to this brand's "
            "audience."
        )

    rtbs = [_as_mapping(r) for r in _as_list(data.get("reasons_to_believe"))]
    if not rtbs:
        issues.append(
            "reasons_to_believe is empty: add 2-4 proof points, each citing a "
            "'src-N' source id or 'brief'."
        )
    uncited = [
        _text(r.get("claim")) or "(empty claim)"
        for r in rtbs
        if not _text(r.get("source_id"))
    ]
    if uncited:
        claims = ", ".join(f"'{c}'" for c in uncited)
        issues.append(
            f"reasons_to_believe without a source_id: {claims}; cite a 'src-N' id "
            "from the research sources or 'brief' for the user's selling points, "
            "or drop the claim."
        )

    bridge = _as_mapping(data.get("trend_bridge"))
    score = bridge.get("fit_score")
    mode = bridge.get("fit_mode")
    if isinstance(score, int) and not isinstance(score, bool):
        expected = _expected_fit_mode(score)
        if mode != expected:
            issues.append(
                f"trend_bridge.fit_mode '{mode}' does not match fit_score {score}; "
                f"use '{expected}' (4-5 direct, 3 cultural, 1-2 light_touch) or "
                "re-score the fit."
            )
    else:
        issues.append(
            "trend_bridge.fit_score is missing: score the brand-trend fit 1-5 and "
            "set fit_mode from it."
        )

    motifs = [_text(m) for m in _as_list(bridge.get("motifs")) if _text(m)]
    if not motifs:
        issues.append(
            "trend_bridge.motifs is empty: add 2-4 concrete motifs specific to the "
            "trend (signature objects, colours, places, events, rituals or memes)."
        )
    elif all(_is_generic_motif(m) for m in motifs):
        issues.append(
            f"trend_bridge.motifs are all generic ({', '.join(motifs)}); replace "
            "them with signature imagery of this specific trend (no phones, feeds, "
            "chat bubbles, hashtags or screens)."
        )

    angles = [_as_mapping(a) for a in _as_list(data.get("angles"))]
    distinct = {_text(a.get("name")).lower() for a in angles} - {""}
    if len(distinct) < 3:
        issues.append(
            f"angles has {len(distinct)} distinct angle name(s); provide 3-5 angles, "
            "each rooted in a different audience tension (not tone variants)."
        )

    assets = [
        a
        for a in _as_list(_as_mapping(data.get("brand")).get("distinctive_assets"))
        if _text(a)
    ]
    if brand_colors.strip() and not assets:
        issues.append(
            f"brand.distinctive_assets is empty although brand colours were given "
            f"('{brand_colors.strip()}'); list the brand's distinctive assets, "
            "including those colours."
        )

    return issues
