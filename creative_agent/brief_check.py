"""Deterministic checks on the structured CreativeBrief.

Pure logic (no ADK imports) behind ``creative_agent.agent.brief_gate``: the
rules the ``CreativeBrief`` output schema cannot express. Each issue string is
specific and actionable because the gate feeds them verbatim to
``brief_reviser`` (as ``brief_issues``) and records any that survive the bounded
revision as ``creative_brief__issues`` (surfaced by
``agent_common.observability.collect_degradation_warnings``).
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .claims import unsupported_claims
from .endorsement import endorsement_terms
from .temporal_terms import temporal_terms
from .text_match import (
    contains_phrase,
    content_tokens,
    mentions,
    same_word,
    words,
)

if TYPE_CHECKING:  # run_policy imports copy_gate, which imports this module
    from .run_policy import RunPolicy

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

# Insight tension: a contrast word, or a ";" followed by one ("Fans want in;
# still, tickets are gone"). A bare ";", a dash or a bare "still" ("fans still
# love it") joins or qualifies clauses without contrasting them.
_TENSION = re.compile(
    r"\b(but|yet|although|though|while|however|despite|instead|except"
    r"|only to|whereas)\b|;\s*(?:yet|but|however|though|still)\b",
    re.IGNORECASE,
)
_AND = re.compile(r"\band\b", re.IGNORECASE)
# A spaced ampersand is a conjunction ("skates & helmets"); a glued one is part
# of a name ("R&B", "M&M's", "AT&T").
_SPACED_AMPERSAND = re.compile(r"\s+&\s+")
# Hyphenated "and" compounds are one idea ("rock-and-roll", "black-and-white").
_AND_COMPOUND = re.compile(r"\w+(?:-\w+)*-and-\w+(?:-\w+)*", re.IGNORECASE)
# An "X and Y" chunk inside a name ("Salt and Vinegar" in "Lay's Salt and
# Vinegar chips").
_NAME_AND_CHUNK = re.compile(r"[\w']+\s+and\s+[\w']+", re.IGNORECASE)
# A sentence break: terminal punctuation, whitespace, then a capital letter, a
# digit, an opening quote, or a word starting with a brand/product word (often
# lowercase-led: "...instrument. iPhone users"). So "No. 1", "U.S.A. for",
# "9 a.m. without" and "2.5x" are one sentence; an ellipsis never breaks.
_SENTENCE_END = re.compile(r"([.!?\u2026]+)\s+(\S)")
_OPENING_QUOTES = frozenset("\"'\u201c\u2018\u00ab")
# Common abbreviations whose period is not a sentence break even before a
# capital ("Dr. Pepper", "St. Louis").
_ABBREVIATIONS = (
    "St.",
    "Dr.",
    "Mr.",
    "Mrs.",
    "Ms.",
    "Jr.",
    "Sr.",
    "Mt.",
    "Approx.",
    "U.S.",
    "U.K.",
    "vs.",
    "e.g.",
    "i.e.",
    "Inc.",
    "Co.",
)
_ABBREVIATION = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(a) for a in sorted(_ABBREVIATIONS, key=len, reverse=True))
    # "No." abbreviates "number" only before a digit ("No. 1"); "said no. Then"
    # is a real break.
    + r"|No\.(?=\s*\d))",
    re.IGNORECASE,
)
# A reason to believe cites a research source ("src-N") or the user's brief;
# see _normalise_source_ids for the tolerated spellings.
_SOURCE_ID = re.compile(r"^(src-\d+|brief)$")
_SRC_VARIANT = re.compile(r"src[\s_-]*0*(\d+)")
_BRIEF_VARIANT = re.compile(r"(?:user\s+)?brief")


def _expected_fit_mode(score: int) -> str:
    if score >= 4:
        return "direct"
    if score == 3:
        return "cultural"
    return "light_touch"


def parse_brief(brief: Mapping[str, Any] | str | None) -> Mapping[str, Any] | None:
    """The brief as a non-empty mapping (a dict or its JSON string), else None."""
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
    # Unicode words (``text_match.words``), so a motif in any script counts;
    # ``same_word`` folds plurals ("phones" is generic).
    tokens = [w for w in words(motif) if w not in _FILLER]
    return not tokens or all(
        any(same_word(w, g) for g in _GENERIC_TOKENS) for w in tokens
    )


def _without_abbreviations(text: str) -> str:
    return _ABBREVIATION.sub(lambda m: m.group(0).replace(".", ""), text)


def _has_sentence_break(text: str, names: tuple[str, ...] = ()) -> bool:
    """A second sentence starts after terminal punctuation (see _SENTENCE_END);
    ``names`` (brand / product) supply the words that start one even in
    lowercase."""
    name_words = {w for n in names for w in words(n) if len(w) >= 2}
    cleaned = _without_abbreviations(text)
    for match in _SENTENCE_END.finditer(cleaned):
        punct, following = match.groups()
        if "\u2026" in punct or ".." in punct:
            continue  # an ellipsis is a pause, not a sentence end
        if following.isupper() or following.isdigit() or following in _OPENING_QUOTES:
            return True
        next_words = words(cleaned[match.start(2) :])
        if next_words and any(next_words[0].startswith(w) for w in name_words):
            return True
    return False


def _with_and(text: str) -> str:
    return _SPACED_AMPERSAND.sub(" and ", text)


def _without_names(text: str, names: tuple[str, ...]) -> str:
    """``text`` with each name, and each "X and Y" chunk of a name, removed.

    Case-insensitive; a spaced "&" counts as "and" on both sides, so "Barnes
    and Noble" is removed for the brand "Barnes & Noble" and "Salt and Vinegar"
    for the product "Lay's Salt and Vinegar chips".
    """
    text = _with_and(text)
    for raw in names:
        name = _with_and(raw.strip())
        if not name:
            continue
        for part in (name, *_NAME_AND_CHUNK.findall(name)):
            text = re.sub(re.escape(part), " ", text, flags=re.IGNORECASE)
    return text


def _joins_with_and(proposition: str, names: tuple[str, ...]) -> bool:
    text = _AND_COMPOUND.sub(" ", _without_names(proposition, names))
    return _AND.search(text) is not None


def _normalise_source_ids(source_id: str) -> list[str]:
    """The cited ids in canonical form; unrecognised parts are kept as written.

    Tolerates case, surrounding brackets, comma-separated lists, "src_3" /
    "src 3" / "src-03" (→ "src-3") and "Brief" / "user brief" (→ "brief").
    """
    ids: list[str] = []
    for raw in source_id.split(","):
        part = re.sub(r"[\[\]()<>{}]", "", raw).strip().lower()
        if not part:
            continue
        if match := _SRC_VARIANT.fullmatch(part):
            ids.append(f"src-{match.group(1)}")
        elif _BRIEF_VARIANT.fullmatch(part):
            ids.append("brief")
        else:
            ids.append(raw.strip())
    return ids or [source_id]


def _normalised(text: str) -> str:
    return " ".join(words(text))


def check_brief(
    brief: Mapping[str, Any] | str | None, policy: RunPolicy | None = None
) -> list[str]:
    """Return the brief's rule violations as actionable issue strings ([] = clean).

    ``brief`` is the ``creative_brief`` state value: a dict (ADK stores parsed
    ``output_schema`` output) or its JSON string; ``policy`` is the run's
    ``RunPolicy`` (None = ``RunPolicy()``, nothing configured). The brief's
    own lists are read from ``brief``, never from the policy (the gate checks
    the brief being written). ``policy.brand_colors`` is the user's optional
    palette; when given, the brief must name distinctive assets.
    ``policy.brand`` / ``target_product`` / ``trend``, and any "X and Y" chunk
    of them, are removed from the proposition before the "and" check (so "Mac
    and Cheese" is not two ideas); hyphenated compounds ("rock-and-roll")
    never count and a spaced "&" counts as "and". Every proposition issue (one
    sentence, "and") is reported, not just the first. ``policy.sources`` is
    the ``sources`` state mapping (keyed by "src-N"); when given (even empty),
    every cited "src-N" must exist in it. Source ids are normalised first (see
    ``_normalise_source_ids``). With a ``target_product``, some mandatory must
    name it exactly (whole words) or mention it per ``text_match.mentions``
    (brand + model tokens count), so the writer cannot narrow it to a variant
    the user never gave. The proposition and the reasons-to-believe claims
    may not carry an absolute claim (``claims.ABSOLUTE_CLAIM_TERMS``:
    "guaranteed", "indestructible", ...) unless
    ``policy.claims_allowed_text`` (the user's key selling points) states it;
    nor may the mandatories (the brief writer writes them, so they never
    license a claim). A non-empty ``trend_bridge.timely_angle`` needs a timely
    ``policy.trend_situation`` (the validated moment, ``timely_ok``) with the
    timely slot on (``policy.max_timely`` > 0), and must not name a result
    the moment contradicts (a win word for a loss, a loss word for a win, any
    result for a moment without one; see ``_contradicting_terms``). Never
    raises: malformed fields are reported as issues.

    With ``policy.safer_angle_active`` (the run opted in on a trend flagged
    sensitive, ``creative_agent.safer_angle``) ``trend_bridge.safer_angle``
    must be set,
    ``trend_bridge.risks`` must list the event's harms, the safer angle may
    share no content word with the risks or ``avoid`` terms (the trend, brand
    and product words excepted) and may carry no endorsement term
    (``endorsement.endorsement_terms``); without it a non-empty
    ``safer_angle`` is stray (see ``_safer_angle_issues``).
    """
    if policy is None:
        # Deferred: run_policy imports copy_gate, which imports this module.
        from .run_policy import RunPolicy

        policy = RunPolicy()
    data = parse_brief(brief)
    if data is None:
        return [MISSING_BRIEF_ISSUE]

    brand = policy.brand
    target_product = policy.target_product
    trend = policy.trend
    issues: list[str] = []

    proposition = _text(data.get("single_minded_proposition"))
    if not proposition:
        issues.append(
            "single_minded_proposition is empty: write ONE sentence carrying the "
            "single idea the audience should take away."
        )
    if proposition and _has_sentence_break(proposition, (brand, target_product)):
        issues.append(
            f"single_minded_proposition must be one sentence; rewrite "
            f"'{proposition}' as a single sentence with a single idea."
        )
    if proposition and _joins_with_and(proposition, (brand, target_product, trend)):
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
    cited = [
        (_text(r.get("claim")) or "(empty claim)", sid)
        for r in rtbs
        if _text(r.get("source_id"))
        for sid in _normalise_source_ids(_text(r.get("source_id")))
    ]
    malformed = [(c, sid) for c, sid in cited if not _SOURCE_ID.match(sid)]
    if malformed:
        listed = ", ".join(f"'{c}' ('{sid}')" for c, sid in malformed)
        issues.append(
            f"reasons_to_believe with an invalid source_id: {listed}; use exactly "
            "'src-N' (a research source id) or 'brief'."
        )
    if (sources := policy.sources) is not None:
        unknown = [
            (c, sid)
            for c, sid in cited
            if sid.startswith("src-") and _SOURCE_ID.match(sid) and sid not in sources
        ]
        if unknown:
            listed = ", ".join(f"'{c}' ('{sid}')" for c, sid in unknown)
            issues.append(
                f"reasons_to_believe cite unknown sources: {listed}; cite only "
                "source ids listed in the research sources, 'brief' for the "
                "user's selling points, or drop the claim."
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
    # Names compared as their joined text_match words: punctuation/case-blind
    # and Unicode-aware, so CJK names are not emptied.
    distinct = {"".join(words(_text(a.get("name")))) for a in angles} - {""}
    if len(distinct) < 3:
        issues.append(
            f"angles has {len(distinct)} distinct angle name(s); provide 3-5 angles, "
            "each rooted in a different audience tension (not tone variants)."
        )
    tensions = [_normalised(_text(a.get("tension"))) for a in angles]
    tensions = [t for t in tensions if t]
    if len(set(tensions)) < len(tensions):
        issues.append(
            "angles repeat the same tension; root each angle in a genuinely "
            "different audience tension."
        )

    assets = [
        a
        for a in _as_list(_as_mapping(data.get("brand")).get("distinctive_assets"))
        if _text(a)
    ]
    brand_colors = policy.brand_colors
    if brand_colors.strip() and not assets:
        issues.append(
            f"brand.distinctive_assets is empty although brand colours were given "
            f"('{brand_colors.strip()}'); list the brand's distinctive assets, "
            "including those colours."
        )

    product = target_product.strip()
    mandatories = [_text(m) for m in _as_list(data.get("mandatories")) if _text(m)]
    claim_text = "\n".join([proposition, *(_text(r.get("claim")) for r in rtbs)])
    # Only the user's selling points license a claim: the brief writer writes
    # the mandatories, so a claim there is checked like the proposition's.
    allowed_claims = policy.claims_allowed_text
    if claims := unsupported_claims(claim_text, allowed_text=allowed_claims):
        issues.append(
            f"Remove unsupported absolute claims ({', '.join(claims)}) unless the "
            "user's selling points state them."
        )
    if claims := unsupported_claims(
        "\n".join(mandatories), allowed_text=allowed_claims
    ):
        issues.append(
            f"mandatories carry unsupported absolute claims ({', '.join(claims)}); "
            "remove them unless the user's selling points state them."
        )
    if product and not any(
        contains_phrase(m, product) or mentions(m, product, brand=brand)
        for m in mandatories
    ):
        issues.append(
            f'Name the product exactly as "{product}" in mandatories; do not add '
            "a variant, finish or model the user did not give."
        )

    issues.extend(
        _timely_angle_issues(
            _text(bridge.get("timely_angle")),
            policy.trend_situation,
            policy.max_timely,
        )
    )
    issues.extend(
        _safer_angle_issues(
            data,
            active=policy.safer_angle_active,
            names=(trend, brand, target_product),
        )
    )
    return issues


# Words too common to signal an event leak when a safer angle shares them with
# the risks ("his music" vs "his murder"); text_match.STOPWORDS are already out.
_OVERLAP_IGNORED = frozenset(
    {
        "he",
        "she",
        "him",
        "his",
        "her",
        "hers",
        "they",
        "them",
        "you",
        "your",
        "we",
        "our",
        "it",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "not",
        "no",
        "all",
        "who",
        "that",
        "after",
        "before",
    }
)


def _overlap(angle: str, terms: Sequence[str], names: Sequence[str]) -> list[str]:
    """Content words of ``angle`` also in ``terms`` (folded, singular/plural
    alike), minus the ``names`` words (trend, brand, product) and pronouns."""
    excluded = [w for name in names for w in words(name)]
    term_words = [w for term in terms for w in words(term)]
    shared: list[str] = []
    for word in content_tokens(angle):
        if (
            word in _OVERLAP_IGNORED
            or word in shared
            or any(same_word(word, other) for other in excluded)
        ):
            continue
        if any(same_word(word, other) for other in term_words):
            shared.append(word)
    return shared


def _safer_angle_issues(
    data: Mapping[str, Any], *, active: bool, names: Sequence[str]
) -> list[str]:
    bridge = _as_mapping(data.get("trend_bridge"))
    angle = _text(bridge.get("safer_angle"))
    if not active:
        if angle:
            return [
                "trend_bridge.safer_angle is set but this run has no safer angle: "
                "clear it (leave it empty)."
            ]
        return []
    issues: list[str] = []
    risks = [_text(r) for r in _as_list(bridge.get("risks")) if _text(r)]
    if not angle:
        issues.append(
            "trend_bridge.safer_angle is empty: set it to ONE respectful, positive "
            "association with the trend's subject from the research (at most 25 "
            "words), never the sensitive event."
        )
    if not risks:
        issues.append(
            "trend_bridge.risks is empty: list the sensitive event's harms (and "
            "short terms for them in avoid)."
        )
    if not angle:
        return issues
    avoid = [_text(a) for a in _as_list(data.get("avoid")) if _text(a)]
    if shared := _overlap(angle, [*risks, *avoid], names):
        issues.append(
            f"trend_bridge.safer_angle shares words with the risks or avoid terms "
            f"({', '.join(shared)}): build it on the subject's positive side, "
            "never the sensitive event."
        )
    if endorsed := endorsement_terms(angle):
        issues.append(
            f"trend_bridge.safer_angle implies endorsement ({', '.join(endorsed)}): "
            "name the subject only as a respectful tribute or inspiration."
        )
    return issues


# Result words by outcome family (temporal_terms output): a timely angle may only
# use its own moment's family.
_WIN_TERMS = frozenset({"won", "win", "wins", "victory", "beat", "beaten"})
_LOSS_TERMS = frozenset({"lost", "loss", "lose", "defeat"})


def _contradicting_terms(angle: str, outcome: str, summary: str = "") -> list[str]:
    """Result words in ``angle`` that the moment's ``outcome`` contradicts.

    A win/loss moment allows its own family. A non-competitive moment (``other``:
    a lottery prize, an award; or a ``draw``) has no family of its own, so it
    allows each family its ``outcome_summary`` uses (a jackpot "won" allows
    "win"), and no others."""
    allowed = {"win": _WIN_TERMS, "loss": _LOSS_TERMS}.get(outcome)
    if allowed is None:
        stated = set(temporal_terms(summary)) if outcome in ("other", "draw") else set()
        allowed = frozenset().union(
            *(family for family in (_WIN_TERMS, _LOSS_TERMS) if stated & family)
        )
    results = _WIN_TERMS | _LOSS_TERMS
    return [t for t in temporal_terms(angle) if t in results and t not in allowed]


def _timely_angle_issues(
    angle: str, situation: Mapping[str, Any] | None, max_timely: int
) -> list[str]:
    if not angle:
        return []
    if not (isinstance(situation, Mapping) and situation.get("timely_ok") is True):
        return [
            "trend_bridge.timely_angle is set without a validated current moment: "
            "clear it (leave it empty)."
        ]
    if max_timely <= 0:
        return [
            "trend_bridge.timely_angle is set but the timely slot is off for this "
            "run (no creative may reference the current moment): clear it (leave "
            "it empty)."
        ]
    outcome = _text(situation.get("outcome"))
    summary = _text(situation.get("outcome_summary"))
    if bad := _contradicting_terms(angle, outcome, summary):
        result = (
            f"outcome: {outcome}" if outcome not in ("", "none") else "no result yet"
        )
        return [
            f"trend_bridge.timely_angle contradicts the current moment "
            f"({', '.join(bad)}; {result}): describe only what the current moment "
            "states."
        ]
    return []


# Wrapping punctuation the brief writer sometimes copies from placeholder
# examples (e.g. "[wrestler likenesses]"). Left in place it silently disables
# literal matching in the copy gate, so term lists are cleaned at the source.
_WRAPPERS = "[]\"'`“”‘’()"
_TERM_LIST_FIELDS = ("avoid", "mandatories")
_BRAND_TERM_FIELDS = ("do_not", "distinctive_assets")
_BRIDGE_TERM_FIELDS = ("motifs",)


def clean_term(term: str) -> str:
    """``term`` without surrounding brackets/quotes and whitespace."""
    cleaned = term.strip()
    while cleaned and cleaned[0] in _WRAPPERS and cleaned[-1] in _WRAPPERS:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _clean_list(items: Any) -> Any:
    if not isinstance(items, list):
        return items
    out = []
    for item in items:
        if isinstance(item, str):
            item = clean_term(item)
            if not item:
                continue
        out.append(item)
    return out


def normalize_brief_terms(brief: Any) -> dict[str, Any] | None:
    """A copy of the brief with its term lists cleaned (``clean_term``), or None
    when the brief is missing or unparseable."""
    parsed = parse_brief(brief)
    if parsed is None:
        return None
    data: dict[str, Any] = copy.deepcopy(dict(parsed))
    for field in _TERM_LIST_FIELDS:
        data[field] = _clean_list(data.get(field))
    brand = data.get("brand")
    if isinstance(brand, dict):
        for field in _BRAND_TERM_FIELDS:
            brand[field] = _clean_list(brand.get(field))
    bridge = data.get("trend_bridge")
    if isinstance(bridge, dict):
        for field in _BRIDGE_TERM_FIELDS:
            bridge[field] = _clean_list(bridge.get(field))
    return data
