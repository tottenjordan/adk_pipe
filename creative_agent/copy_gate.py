"""Deterministic checks on the ad copy critic's final copies.

Pure logic (no ADK imports) behind ``creative_agent.agent.copy_gate``. Gating
policy (each needless revision costs a worker LLM call, latency and possibly a
user-visible warning, so false positives are kept low):

* **deterministic** issues — the rules the ``FinalAdCopyList`` output schema
  cannot express (product named, CTA, lengths, the brief's avoid terms,
  unsupported absolute claims such as "guaranteed" / "indestructible", and
  with the run's trend a trend tie via ``trend_tie.has_trend_tie``, skipped
  for a sensitive moment unless the run takes a safer angle, which also
  flags endorsement wording, see below) — gate a revision AND, if they
  survive it, are recorded as residual issues;
* **self_reported** issues — the critic's own failed ``brief_checks`` — gate a
  revision only for ``proposition``, ``mandatories``, ``risks`` and
  ``trend_bridge`` (the brief's hard contract, its trend risks and a clear,
  specific trend tie — the pre-judge counterpart of the judge's blocking
  ``trend_connected`` gate — none with a deterministic check), and are NEVER
  recorded as residual issues. Every other failed item is advisory: it stays in ``brief_checks``
  (UI/eval) and never gates.
* **structural** issues (``structural_issues``: fewer than ``EXPECTED_COPIES``
  copies; with a brief, copies whose ``brief_checks`` lack a gating item) are
  list-level
  problems the per-copy reviser cannot fix (``restore_unflagged`` drops any
  copy it adds), so they never gate; the gate records them with the residual
  issues on its "ok" exit.

Rating strictness (opt-in rating learning, ``rating_strictness`` in state;
default none) only ever tightens: ``weak_cta`` caps the CTA at
``STRICT_CTA_WORDS`` (6) words, and ``off_brief`` also gates the critic's
failed ``reason_to_believe`` self-report (``OFF_BRIEF_BRIEF_CHECKS``; still
never recorded as residual issues; ``trend_bridge`` already gates).

Safer angle (``safer_angle_active``: the user picked a sensitive trend and
opted in to a respectful, positive angle on its subject): any copy field with
an ``endorsement.endorsement_terms`` hit is flagged (the real person never
endorses or speaks for the product), and the trend tie is checked again even
for a sensitive moment (connecting through the safer angle is the point), with
the trend words that the brief's ``trend_bridge.risks`` name (the sensitive
event: "murder", "trial") never counting as a tie unless the brief's
``trend_bridge.safer_angle`` uses them (``safer_angle_tie_avoid``).
``safer_angle_named_copies`` lists the copies that name the trend's subject.
At least half of the copies (ceil(n/2)) must tie to the safer angle: name the
subject or use one of ``safer_angle_tie_words`` (the angle's distinctive
words; generic words like "soul" / "studio", product / brand words and risk /
avoid words never count). Below that, EVERY untied copy is flagged (the
simplest deterministic rule: no choice of which copies to rewrite).

Matching (product named, avoid terms) uses ``text_match``: accent-folded,
Unicode-aware, plural-insensitive (s / es / ies↔y), and brands written with
punctuation or digits ("AT&T", "M&M's", "7UP") also match their
punctuation-stripped forms. A product with nothing matchable ("GE") is never
flagged; the brand name alone does not count as naming the product (the
judge's ``product_named`` gate), but any significant product word does
(lenient: looser than the image-prompt guard, which needs a brand anchor in
the product match). Avoid terms contained in the product, a non-negative
mandatory, or a Title-Case name in the trend are exempt; lowercase trend words
("shooting", "death") never are.

Each issue text is specific and actionable because the gate feeds them
verbatim to ``ad_copy_reviser`` (as ``ad_copy_issues``); ``residual_issues``
picks the ones recorded as ``ad_copy_critique__issues`` (surfaced by
``agent_common.observability.collect_degradation_warnings``).

``restore_unflagged`` is the reviser's safety net: the reviser may only rewrite
the copies the gate flagged, so every other copy (and any copy it dropped or
duplicated) is put back to its pre-revision value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .brief_check import clean_term, parse_brief
from .claims import unsupported_claims
from .endorsement import endorsement_terms
from .temporal_terms import temporal_terms
from .text_match import (
    PACKAGING_WORDS,
    contains_phrase,
    content_tokens,
    fold,
    same_word,
    words,
)
from .trend_tie import distinctive_trend_tokens, has_trend_tie, tie_terms

if TYPE_CHECKING:  # run_policy imports this module (the brief term readers)
    from .run_policy import RunPolicy

MAX_HEADLINE_CHARS = 60
MAX_CAPTION_CHARS = 2200
MAX_CTA_WORDS = 8
# Rating strictness (``rating_strictness``, opt-in rating learning): a
# recurring "weak call to action" fail reason tightens the CTA limit.
STRICT_CTA_WORDS = 6
# Avoid entries longer than this are sentences ("never mention falling off
# cliffs"), not terms: a literal match would never fire, so they are skipped
# (the critic/reviser still read the full avoid list in the brief).
MAX_AVOID_TERM_WORDS = 4

# Final copies the critic must select (CREATIVE prompts: "exactly 4").
EXPECTED_COPIES = 4
# Words that never identify a product on their own ("The New Rocket Skates").
_STOPWORDS = frozenset(
    {
        "a",
        "all",
        "an",
        "and",
        "at",
        "by",
        "for",
        "from",
        "in",
        "its",
        "new",
        "of",
        "on",
        "or",
        "our",
        "the",
        "to",
        "with",
        "your",
    }
)
_MIN_TOKEN_CHARS = 3
# Self-reported brief_checks items that gate a revision: the brief's hard
# contract, its trend risks (never joking about a real disaster) and the trend
# tie (a clear, specific connection a trend follower would recognise: the
# judge's blocking trend_connected gate, since 2026-10-12.2), which no
# deterministic check covers. The rest are advisory.
_GATING_ORDER = ("proposition", "mandatories", "risks", "trend_bridge")
GATING_BRIEF_CHECKS = frozenset(_GATING_ORDER)
# Items a deterministic check already covers; a self-reported failure of one is
# never listed (no double listing), even if it were made gating.
DETERMINISTIC_BRIEF_CHECKS = frozenset({"product", "avoid", "cta"})
# Rating strictness: a recurring "off-brief / wrong message" fail reason also
# makes the critic's failed reason to believe gate (proposition and the trend
# bridge already do). ``tone`` stays advisory (the off_brand_tone reason is
# guidance only).
OFF_BRIEF_BRIEF_CHECKS = frozenset({"reason_to_believe"})


def max_cta_words(strictness: Sequence[str] = ()) -> int:
    """The CTA word limit: ``STRICT_CTA_WORDS`` under ``weak_cta`` strictness."""
    return STRICT_CTA_WORDS if "weak_cta" in strictness else MAX_CTA_WORDS


def gating_brief_checks(strictness: Sequence[str] = ()) -> frozenset[str]:
    """The self-reported brief_checks items that gate (plus the reason to
    believe under ``off_brief`` strictness)."""
    if "off_brief" in strictness:
        return GATING_BRIEF_CHECKS | OFF_BRIEF_BRIEF_CHECKS
    return GATING_BRIEF_CHECKS


IssueKind = Literal["deterministic", "self_reported"]


@dataclass(frozen=True)
class CopyIssue:
    """One gate issue: its reviser-facing ``text`` and where it came from."""

    kind: IssueKind
    text: str

    def __str__(self) -> str:
        return self.text


# Every copy field the product may be named in (the CTA counts too).
_COPY_FIELDS = ("headline", "body_text", "social_caption", "call_to_action")


def parse_copies(copies: Any) -> list[Mapping[str, Any]]:
    """The copies as a list of mappings.

    Accepts a list of copy dicts, the ``FinalAdCopyList`` dict
    (``{"ad_copies": [...]}``) or a JSON string of either; anything else (or a
    malformed entry) is dropped, so this never raises.
    """
    if isinstance(copies, str):
        try:
            copies = json.loads(copies)
        except ValueError:
            return []
    if isinstance(copies, Mapping):
        copies = copies.get("ad_copies")
    if not isinstance(copies, list):
        return []
    return [c for c in copies if isinstance(c, Mapping)]


def item_keys(items: Sequence[Mapping[str, Any]], id_field: str) -> list[str]:
    """Each item's issue key, unique within ``items``.

    ``str(item[id_field])``; ``#<index>`` for an item without one; and
    ``<id>#<n>`` for the n-th (n >= 2) item repeating an id, so a model that
    duplicated an id never gets two items collapsed into one by a gate or by
    ``restore_unflagged_items``.
    """
    keys: list[str] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(items):
        item_id = item.get(id_field)
        if item_id is None or isinstance(item_id, bool):
            keys.append(f"#{index}")
            continue
        base = str(item_id)
        seen[base] = seen.get(base, 0) + 1
        keys.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return keys


def copy_keys(copies: Sequence[Mapping[str, Any]]) -> list[str]:
    """Each copy's issue key (``item_keys`` on ``original_id``)."""
    return item_keys(copies, "original_id")


def _brief_list(brief: Mapping[str, Any] | str | None, field: str) -> list[str]:
    data = parse_brief(brief)
    if data is None:
        return []
    items = data.get(field)
    if not isinstance(items, list):
        return []
    terms = (clean_term(a) for a in items if isinstance(a, str))
    return [t for t in terms if t]


def brief_avoid(brief: Mapping[str, Any] | str | None) -> list[str]:
    """The structured brief's ``avoid`` list (brief dict or its JSON string)."""
    return _brief_list(brief, "avoid")


def brief_mandatories(brief: Mapping[str, Any] | str | None) -> list[str]:
    """The structured brief's ``mandatories`` list (brief dict or its JSON string)."""
    return _brief_list(brief, "mandatories")


def brief_motifs(brief: Any) -> list[str]:
    """The structured brief's ``trend_bridge.motifs`` (brief dict, its JSON
    string, or a dict holding a ``TrendBridge`` model); [] when missing."""
    motifs = _bridge_field(brief, "motifs")
    if not isinstance(motifs, list):
        return []
    return [m.strip() for m in motifs if isinstance(m, str) and m.strip()]


def _bridge_field(brief: Any, field: str) -> Any:
    data = parse_brief(brief)
    bridge = data.get("trend_bridge") if data is not None else None
    if hasattr(bridge, "model_dump"):
        bridge = bridge.model_dump()
    return bridge.get(field) if isinstance(bridge, Mapping) else None


def brief_risks(brief: Any) -> list[str]:
    """The structured brief's ``trend_bridge.risks``; [] when missing."""
    risks = _bridge_field(brief, "risks")
    if not isinstance(risks, list):
        return []
    return [r.strip() for r in risks if isinstance(r, str) and r.strip()]


def brief_safer_angle(brief: Any) -> str:
    """The structured brief's ``trend_bridge.safer_angle``; "" when missing."""
    angle = _bridge_field(brief, "safer_angle")
    return angle.strip() if isinstance(angle, str) else ""


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


def safer_angle_named_copies(
    copies: Any,
    *,
    trend_terms: str,
    avoid: Sequence[str] = (),
    risks: Sequence[str] = (),
    safer_angle: str = "",
) -> list[str]:
    """The keys (``copy_keys``) of the copies whose text names the trend's
    subject: the trend phrase or one of its distinctive words, minus the
    ``safer_angle_tie_avoid`` words (motifs do not count). [] without a trend."""
    if not words(trend_terms):
        return []
    tie_avoid = safer_angle_tie_avoid(
        trend_terms, avoid=avoid, risks=risks, safer_angle=safer_angle
    )
    parsed = parse_copies(copies)
    named: list[str] = []
    for key, copy in zip(copy_keys(parsed), parsed, strict=True):
        text = " ".join(_text(copy.get(f)) for f in _COPY_FIELDS)
        if has_trend_tie(text, trend=trend_terms, avoid=tie_avoid):
            named.append(key)
    return named


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


def _safer_angle_untied(
    parsed: list[Mapping[str, Any]], policy: RunPolicy, avoid: Sequence[str]
) -> dict[str, str]:
    """The safer-angle tie issue per untied copy key, when fewer than
    ceil(n/2) copies tie (name the subject, as ``safer_angle_named_copies``,
    or use a ``safer_angle_tie_words`` word); {} otherwise or without a
    trend. ``avoid`` is the gate's filtered avoid terms."""
    trend_terms = policy.trend
    target_product = policy.target_product
    brand = policy.brand
    risks = policy.risks
    safer_angle = policy.safer_angle_text
    if not parsed or not words(trend_terms):
        return {}
    named = set(
        safer_angle_named_copies(
            parsed,
            trend_terms=trend_terms,
            avoid=avoid,
            risks=risks,
            safer_angle=safer_angle,
        )
    )
    angle_words = safer_angle_tie_words(
        safer_angle,
        trend_terms=trend_terms,
        target_product=target_product,
        brand=brand,
        avoid=avoid,
        risks=risks,
    )
    untied: list[str] = []
    for key, copy in zip(copy_keys(parsed), parsed, strict=True):
        text_words = words(" ".join(_text(copy.get(f)) for f in _COPY_FIELDS))
        if key not in named and not any(
            same_word(a, w) for a in angle_words for w in text_words
        ):
            untied.append(key)
    need = -(-len(parsed) // 2)
    if len(parsed) - len(untied) >= need:
        return {}
    tie_avoid = safer_angle_tie_avoid(
        trend_terms, avoid=avoid, risks=risks, safer_angle=safer_angle
    )
    examples = ", ".join(
        [
            *tie_terms(trend_terms, avoid=tie_avoid)[:1],
            *(w.title() for w in angle_words),
        ][:_MAX_SAFER_EXAMPLES]
    )
    angle = f" ({_short_angle(safer_angle)})" if safer_angle.strip() else ""
    issue = (
        f"not tied to the safer angle{angle}: name the subject in tribute or use "
        "a distinctive part of its legacy, not just generic words"
        + (f" (e.g. {examples})" if examples else "")
        + f"; at least {need} of the {len(parsed)} copies must."
    )
    return dict.fromkeys(untied, issue)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def product_words(target_product: str) -> list[str]:
    """The product's significant words (``text_match.words``: folded, possessives
    stripped).

    Skips stopwords, generic packaging words ("can", "pack": "you can" or
    "pack your bags" never names a product), pure numbers (model numbers,
    years) and words under 3 characters: "PRS SE Custom 24" yields ["prs",
    "custom"]. Brand words are
    kept: when the product phrase contains the brand ("Powerball tickets"),
    naming the brand names the product.
    """
    return [
        word
        for word in words(target_product)
        if len(word) >= _MIN_TOKEN_CHARS
        and word not in _STOPWORDS
        and word not in PACKAGING_WORDS
        and not word.isdigit()
    ]


def _brand_forms(target_product: str) -> list[str]:
    """Punctuation-stripped forms of the product's punctuated/alphanumeric chunks.

    A whitespace-separated chunk with inner punctuation ("AT&T", "M&M's",
    "Coca-Cola") or letters mixed with digits ("7UP", "3M", "CE24") is also
    compared without its punctuation, so "AT&T." / "M&Ms" / "7 Up" name it.
    """
    forms: list[str] = []
    for chunk in target_product.split():
        parts = words(chunk)
        joined = "".join(parts)
        mixed = any(c.isdigit() for c in joined) and any(c.isalpha() for c in joined)
        if len(joined) >= 2 and not joined.isdigit() and (len(parts) > 1 or mixed):
            forms.append(joined)
    return forms


def _copy_forms(copy_text: str) -> set[str]:
    """The copy's chunks without punctuation, plus adjacent word pairs joined."""
    forms = {"".join(words(chunk)) for chunk in copy_text.split()}
    copy_words = words(copy_text)
    forms.update(a + b for a, b in zip(copy_words, copy_words[1:], strict=False))
    forms.discard("")
    return forms


def _names_product(copy_text: str, target_product: str) -> bool:
    """The copy names the product: its full phrase, ANY significant word, or a
    punctuation-stripped brand form (see ``_brand_forms``).

    Deliberately lenient (each false positive costs a revision call and a
    user-visible warning): "Lace up your skates" names "Rocket Skates" and
    "the PRS SE CE24" names "SE CE24 Electric Guitar". A brand outside the
    product phrase never does: "Only on Apple" does not name "iPhone 16 Pro"
    (the judge's ``product_named`` gate: the brand name alone does not
    count). (The image-prompt guard is stricter: there a partial product
    match must carry a brand token, see ``concept_guard``.) Accent-, case-
    and plural-insensitive (s / es / ies↔y).
    """
    if contains_phrase(copy_text, target_product):
        return True
    copy_words = set(words(copy_text))
    if any(
        same_word(word, other)
        for word in product_words(target_product)
        for other in copy_words
    ):
        return True
    copy_forms = _copy_forms(copy_text)
    return any(
        same_word(form, other)
        for form in _brand_forms(target_product)
        for other in copy_forms
    )


def _has_matchable_tokens(target_product: str) -> bool:
    """The product yields something to look for (else the check is skipped)."""
    return bool(product_words(target_product) or _brand_forms(target_product))


# A mandatory phrased as a prohibition ("Never show children drinking") does
# not license its words.
_NEGATIVE = re.compile(r"\b(?:never|no|not|avoid|without)\b|n't\b")
# Lowercase words allowed inside a Title-Case name ("Lord of the Rings").
_NAME_CONNECTORS = frozenset({"of", "the", "and", "&", "de", "la", "le", "von", "van"})


def is_negative(text: str) -> bool:
    """``text`` is phrased as a prohibition (never / no / not / don't / avoid /
    without)."""
    return _NEGATIVE.search(fold(text)) is not None


def _is_title(token: str) -> bool:
    letters = [c for c in token if c.isalpha()]
    return bool(letters) and letters[0].isupper()


def name_chunks(trend: str) -> list[str]:
    """The proper-name chunks of ``trend``: runs of consecutive Title-Case words
    (lowercase connectors like "of the" allowed inside a run), from the
    ORIGINAL casing. "Charlie Kirk shooting" → ["Charlie Kirk"]; "Lord of the
    Rings remake" → ["Lord of the Rings"]. A lone capitalised first word
    ("Death of a legend") is sentence case, not a name, unless it is the whole
    trend; an all-lowercase trend has no names.
    """
    tokens = trend.split()
    chunks: list[list[str]] = []
    run: list[str] = []
    pending: list[str] = []
    starts: list[int] = []
    for index, token in enumerate(tokens):
        if _is_title(token):
            if run:
                run.extend(pending)
            else:
                starts.append(index)
            run.append(token)
            pending = []
        elif run and token.lower() in _NAME_CONNECTORS:
            pending.append(token)
        else:
            if run:
                chunks.append(run)
            run, pending = [], []
    if run:
        chunks.append(run)
    return [
        " ".join(chunk)
        for chunk, start in zip(chunks, starts, strict=True)
        if not (start == 0 and len(chunk) == 1 and len(tokens) > 1)
    ]


def _avoid_terms(
    avoid: Iterable[str] | str,
    *,
    product: str = "",
    mandatories: Iterable[str] = (),
    trend: str = "",
) -> list[str]:
    """The avoid entries to match literally.

    Skips entries over MAX_AVOID_TERM_WORDS words, and entries the copy must be
    allowed to say: contained in the product name ("sugar" in "Coca-Cola Zero
    Sugar"), in a non-negative brief mandatory ("gambling" when a mandatory
    requires the problem-gambling helpline; never via "Never show children
    drinking"), or in a proper-name chunk of the trend (``name_chunks``:
    "Taylor Swift" in "Taylor Swift wedding"). Lowercase trend words
    ("shooting" in "Charlie Kirk shooting", "death") never exempt a term.
    """
    if isinstance(avoid, str):
        avoid = re.split(r"[\n;,]", avoid)
    terms = [t.strip() for t in avoid if isinstance(t, str) and t.strip()]
    sources = [product] if isinstance(product, str) and product.strip() else []
    sources += [
        m
        for m in mandatories
        if isinstance(m, str) and m.strip() and not is_negative(m)
    ]
    sources += name_chunks(trend) if isinstance(trend, str) else []
    return [
        t
        for t in terms
        if len(words(t)) <= MAX_AVOID_TERM_WORDS
        and not any(contains_phrase(src, t) for src in sources)
    ]


def _clear_timely(item: dict[str, Any], why: str) -> None:
    item["uses_trend_state"] = False
    reason = str(item.get("timely_reason") or "").strip()
    item["timely_reason"] = f"{reason} (Not timely: {why}.)".strip()


def cap_timely_flags(
    items: Sequence[Mapping[str, Any]], *, timely_ok: bool, max_timely: int
) -> tuple[list[dict[str, Any]], list[int]]:
    """Copies of ``items`` with ``uses_trend_state`` cleared beyond the cap.

    Keeps the first ``max_timely`` flagged items (``uses_trend_state is True``)
    in order and clears the rest with a "(Not timely: cap reached.)" suffix on
    ``timely_reason``; clears every flag ("no validated current moment") when
    not ``timely_ok``. A non-bool flag normalises to False. Returns the items
    and the indexes whose flag was cleared. Shared by the concept and copy caps.
    """
    out: list[dict[str, Any]] = []
    cleared: list[int] = []
    kept = 0
    for index, item in enumerate(items):
        c = deepcopy(dict(item))
        flag = c.get("uses_trend_state")
        if flag is not True:
            if flag not in (None, False):
                c["uses_trend_state"] = False
            out.append(c)
            continue
        if not timely_ok:
            _clear_timely(c, "no validated current moment")
            cleared.append(index)
        elif kept >= max_timely:
            _clear_timely(c, "cap reached")
            cleared.append(index)
        else:
            kept += 1
        out.append(c)
    return out, cleared


@dataclass(frozen=True)
class _CopyChecks:
    """The per-run inputs every copy check shares, derived once by
    ``gate_copies``: the policy, the avoid terms to match literally
    (``_avoid_terms``), whether the timely cap has room, and the terms that
    never count as a trend tie."""

    policy: RunPolicy
    avoid: list[str]
    timely_room: bool
    tie_avoid: list[str]


def _deterministic_issues(copy: Mapping[str, Any], checks: _CopyChecks) -> list[str]:
    policy = checks.policy
    target_product = policy.target_product
    max_cta = max_cta_words(policy.strictness)
    trend_terms = policy.trend
    issues: list[str] = []
    copy_text = " ".join(_text(copy.get(f)) for f in _COPY_FIELDS)

    if _has_matchable_tokens(target_product) and not _names_product(
        copy_text, target_product
    ):
        issues.append(
            f"product not named: mention '{target_product.strip()}' in the "
            "headline, body text, social caption or call to action."
        )

    cta = _text(copy.get("call_to_action"))
    if not cta:
        issues.append(
            "call_to_action is empty: add a specific CTA that starts with an "
            f"action verb (at most {max_cta} words)."
        )
    elif (n := len(cta.split())) > max_cta:
        issues.append(
            f"call_to_action has {n} words ('{cta}'): cut it to at most "
            f"{max_cta} words, starting with an action verb."
        )

    headline = _text(copy.get("headline"))
    if len(headline) > MAX_HEADLINE_CHARS:
        issues.append(
            f"headline is {len(headline)} characters ('{headline}'): shorten it "
            f"to at most {MAX_HEADLINE_CHARS} characters."
        )

    caption = _text(copy.get("social_caption"))
    if len(caption) > MAX_CAPTION_CHARS:
        issues.append(
            f"social_caption is {len(caption)} characters: shorten it to at most "
            f"{MAX_CAPTION_CHARS} characters."
        )

    if claims := unsupported_claims(copy_text, allowed_text=policy.claims_allowed_text):
        issues.append(
            f"unsupported absolute claim ({', '.join(claims)}): remove it or "
            "state a verifiable benefit instead, unless the user's selling "
            "points state it."
        )

    # The timely slot: with a timely moment, an evergreen copy must stay
    # timeless (no dates, results or "tonight"); the timely copy may not.
    # Making it the timely copy is offered only while the cap has room.
    if policy.timely_ok and copy.get("uses_trend_state") is not True:
        if terms := temporal_terms(copy_text):
            fix = (
                "remove it or make this the timely copy."
                if checks.timely_room
                else ("remove it.")
            )
            issues.append(
                f"evergreen copy mentions a dated or outcome term "
                f"({', '.join(terms)}): {fix}"
            )

    if policy.safer_angle_active and (terms := endorsement_terms(copy_text)):
        issues.append(
            f"implies endorsement or speaks for the real person "
            f"({', '.join(terms)}): remove it."
        )

    for term in checks.avoid:
        if contains_phrase(copy_text, term):
            issues.append(
                f"contains the avoided term '{term}': remove it or rephrase "
                "without it (the brief's avoid list)."
            )

    # The trend tie (pre-judge counterpart of the judge's blocking
    # trend_connected gate): never for a sensitive moment, whose event the
    # creative work must not reference, unless the run takes a safer angle
    # (then the event's words never count, ``tie_avoid``); an avoid term never
    # counts as a tie.
    if (
        trend_terms.strip()
        and (policy.safer_angle_active or not policy.trend_sensitive)
        and not has_trend_tie(
            copy_text, trend=trend_terms, motifs=policy.motifs, avoid=checks.tie_avoid
        )
    ):
        examples = ", ".join(
            tie_terms(trend_terms, policy.motifs, avoid=checks.tie_avoid)
        )
        issues.append(
            "not clearly tied to the trend: name it or one of its motifs"
            + (f" (e.g. {examples})" if examples else "")
        )
    return issues


def _self_reported_issues(
    copy: Mapping[str, Any], gating: frozenset[str] = GATING_BRIEF_CHECKS
) -> list[str]:
    """The critic's failed brief_checks items that gate (``gating``)."""
    issues: list[str] = []
    checks = copy.get("brief_checks")
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, Mapping) or check.get("passed") is not False:
            continue
        item = _text(check.get("item"))
        if item not in gating or item in DETERMINISTIC_BRIEF_CHECKS:
            continue
        note = _text(check.get("note"))
        issues.append(
            f"brief check failed: {item} — {note}"
            if note
            else f"brief check failed: {item}"
        )
    return issues


def _check_copy(copy: Mapping[str, Any], checks: _CopyChecks) -> list[CopyIssue]:
    deterministic = _deterministic_issues(copy, checks)
    self_reported = _self_reported_issues(
        copy, gating_brief_checks(checks.policy.strictness)
    )
    return [CopyIssue("deterministic", text) for text in deterministic] + [
        CopyIssue("self_reported", text) for text in self_reported
    ]


def gate_copies(copies: Any, policy: RunPolicy) -> dict[str, list[CopyIssue]]:
    """The copies' gating issues, keyed by ``copy_keys``.

    ``copies`` is the ``ad_copy_critique`` state value (see ``parse_copies``);
    ``policy`` is the run's ``RunPolicy``. Deterministic checks, per copy: the
    product is named (``policy.target_product``, ANY of its significant words,
    or a punctuation-stripped brand form; ``policy.brand`` alone never counts:
    it is used only by the safer-angle tie; accent/case/plural-insensitive,
    see ``_names_product``) in the headline/body/caption/CTA — skipped when
    the product yields nothing to match; the CTA is non-empty and at most 8
    words; the headline is at most 60 characters; the social caption at most
    2200; no term from ``policy.avoid`` (the creative brief's avoid list; a
    string is split on newlines/commas/semicolons) appears as a whole
    word/phrase (entries over ``MAX_AVOID_TERM_WORDS`` words, and entries
    contained in the product name, a non-negative brief mandatory or a
    proper-name chunk of ``policy.trend``, are skipped; see ``_avoid_terms``);
    no unsupported absolute claim (``claims.ABSOLUTE_CLAIM_TERMS``) that
    ``policy.claims_allowed_text`` (the user's key selling points) does not
    state. Self-reported: each failed ``brief_checks`` item in
    ``GATING_BRIEF_CHECKS``. ``policy.strictness`` (the run's
    ``rating_strictness`` flags; default none = the rules above) only
    tightens: ``weak_cta`` caps the CTA at ``STRICT_CTA_WORDS`` words and
    ``off_brief`` adds ``OFF_BRIEF_BRIEF_CHECKS`` to the gating items. Only
    copies with issues are returned ({} = clean). ``policy.timely_ok`` (the
    run has a timely moment and the slot is on) adds one deterministic check:
    an evergreen copy (``uses_trend_state`` not True) must contain no dated or
    outcome term (``temporal_terms``; the fix offers "make this the timely
    copy" only while fewer than ``policy.max_timely`` copies are timely);
    without it the checks are unchanged. ``policy.trend`` (default "" = off)
    adds the trend-tie check, appended last: a copy that names none of the
    trend's distinctive words, the trend phrase or one of ``policy.motifs``
    (the brief's ``trend_bridge.motifs``) is flagged
    (``trend_tie.has_trend_tie``; an avoid term never counts as a tie);
    skipped when ``policy.trend_sensitive`` (the current moment is sensitive
    or trend_scout flagged the trend, so it must not be referenced).
    ``policy.safer_angle_active`` (the run takes a safer angle on a sensitive
    trend; default False = the checks above, unchanged) flags any endorsement
    term (``endorsement.endorsement_terms``) and checks the trend tie even
    when ``trend_sensitive``, never counting the trend words that
    ``policy.risks`` (the brief's ``trend_bridge.risks``) name unless
    ``policy.safer_angle_text`` (its ``trend_bridge.safer_angle``) uses them
    (``safer_angle_tie_avoid``), and requires at least ceil(n/2) copies to
    tie to the safer angle (name the subject or use a
    ``safer_angle_tie_words`` word; brand words never count), else every
    untied copy gets a deterministic issue, appended last. Never raises.
    """
    terms = _avoid_terms(
        policy.avoid,
        product=policy.target_product,
        mandatories=policy.mandatories,
        trend=policy.trend,
    )
    parsed = parse_copies(copies)
    active = policy.safer_angle_active
    checks = _CopyChecks(
        policy=policy,
        avoid=terms,
        timely_room=sum(c.get("uses_trend_state") is True for c in parsed)
        < policy.max_timely,
        tie_avoid=(
            safer_angle_tie_avoid(
                policy.trend,
                avoid=terms,
                risks=policy.risks,
                safer_angle=policy.safer_angle_text,
            )
            if active
            else list(terms)
        ),
    )
    untied = _safer_angle_untied(parsed, policy, terms) if active else {}
    issues: dict[str, list[CopyIssue]] = {}
    for key, copy in zip(copy_keys(parsed), parsed, strict=True):
        found = _check_copy(copy, checks)
        if key in untied:
            found.append(CopyIssue("deterministic", untied[key]))
        if found:
            issues[key] = found
    return issues


def enforce_timely_copy_cap(
    critique: Any, *, timely_ok: bool, max_timely: int
) -> tuple[Any, list[str]]:
    """The ``ad_copy_critique`` value with ``uses_trend_state`` cleared beyond
    the timely cap (first ``max_timely`` kept, copy order) or everywhere when
    the run has no timely moment, plus one warning per cleared copy.

    Returns the SAME value (and no warnings) when nothing changes; otherwise a
    new value in the same shape (a dict, or a JSON string of one). The copy
    gate applies it before checking, so the reviser and the eval see the capped
    flags.
    """
    value = critique
    if isinstance(critique, str):
        try:
            value = json.loads(critique)
        except ValueError:
            return critique, []
    if not isinstance(value, Mapping):
        return critique, []
    copies = value.get("ad_copies")
    if not isinstance(copies, list) or not all(isinstance(c, Mapping) for c in copies):
        return critique, []
    capped, cleared = cap_timely_flags(
        copies, timely_ok=timely_ok, max_timely=max_timely
    )
    if capped == [dict(c) for c in copies]:
        return critique, []
    why = "cap reached" if timely_ok else "no validated current moment"
    warns = [
        f"copy {capped[i].get('original_id', '?')}: timely flag cleared ({why})"
        for i in cleared
    ]
    new_value = {**value, "ad_copies": capped}
    return (json.dumps(new_value) if isinstance(critique, str) else new_value), warns


def structural_issues(
    copies: Any, expected: int = EXPECTED_COPIES, *, has_brief: bool = True
) -> list[str]:
    """List-level problems the per-copy reviser cannot fix (warning-only).

    * fewer than ``expected`` copies (the reviser may only rewrite flagged
      copies and ``restore_unflagged`` drops any copy it adds, so a revision
      round could never restore a missing one);
    * with a brief (``has_brief``), copies whose ``brief_checks`` lack a
      ``GATING_BRIEF_CHECKS`` item (the hard contract was never self-checked,
      so the gate cannot route on it), collapsed into one line. Without a
      brief there is no checklist to apply, so no note. Advisory items are not
      required here.

    The gate records these with the residual issues on its "ok" exit; they
    never route a revision. ``[]`` when there are no copies at all (nothing
    was produced: that is the producer's degradation, not this list's).
    """
    parsed = parse_copies(copies)
    if not parsed:
        return []
    notes: list[str] = []
    if len(parsed) < expected:
        notes.append(f"only {len(parsed)} of {expected} ad copies were produced.")
    if not has_brief:
        return notes
    gaps = 0
    for copy in parsed:
        checks = copy.get("brief_checks")
        present = {
            _text(c.get("item"))
            for c in (checks if isinstance(checks, list) else [])
            if isinstance(c, Mapping)
        }
        gaps += any(i not in present for i in _GATING_ORDER)
    if gaps:
        notes.append(
            f"{gaps} of {len(parsed)} ad copies lack the "
            f"{'/'.join(_GATING_ORDER)} brief check."
        )
    return notes


def residual_issues(
    issues: Mapping[str, Sequence[CopyIssue]],
) -> dict[str, list[CopyIssue]]:
    """Only the deterministic issues (the ones recorded if they survive)."""
    residual = {
        key: [i for i in items if i.kind == "deterministic"]
        for key, items in issues.items()
    }
    return {key: items for key, items in residual.items() if items}


def _copy_label(copies: list[Mapping[str, Any]], key: str) -> str:
    for copy_id, copy in zip(copy_keys(copies), copies, strict=True):
        if copy_id == key:
            headline = _text(copy.get("headline"))
            return f'Copy {key} ("{headline}")' if headline else f"Copy {key}"
    return f"Copy {key}"


def format_copy_issues(copies: Any, issues: Mapping[str, Sequence[object]]) -> str:
    """The issues as a Markdown list grouped per copy (the reviser's input)."""
    parsed = parse_copies(copies)
    lines: list[str] = []
    for key, items in issues.items():
        lines.append(f"- **{_copy_label(parsed, key)}:**")
        lines.extend(f"  - {item}" for item in items)
    return "\n".join(lines)


def user_revision_inputs(copies: Any, feedback: str) -> dict[str, Any] | None:
    """The ad copy reviser's inputs for a user revision of EVERY copy.

    Interactive checkpoint 2 reuses ``ad_copy_reviser`` with free-text user
    feedback that applies to all copies, so every copy is flagged with it: the
    returned state delta holds ``ad_copy_feedback``, the per-copy
    ``ad_copy_issues`` list, all copy keys as ``ad_copy_flagged_ids`` (so the
    reviser's ``restore_unflagged`` safety net keeps every revised copy) and the
    pre-revision snapshot ``ad_copy_critique__before_revision``. None when
    there are no copies.
    """
    parsed = parse_copies(copies)
    if not parsed:
        return None
    keys = copy_keys(parsed)
    note = f"Apply the user's feedback: {feedback}"
    return {
        "ad_copy_feedback": feedback,
        "ad_copy_issues": format_copy_issues(parsed, {key: [note] for key in keys}),
        "ad_copy_flagged_ids": keys,
        "ad_copy_critique__before_revision": {"ad_copies": deepcopy(parsed)},
    }


def flatten_copy_issues(
    copies: Any, issues: Mapping[str, Sequence[object]]
) -> list[str]:
    """One string per issue, prefixed with its copy (the residual-issue record)."""
    parsed = parse_copies(copies)
    return [
        f"{_copy_label(parsed, key)}: {item}"
        for key, items in issues.items()
        for item in items
    ]


def restore_unflagged_items(
    old: Sequence[Mapping[str, Any]],
    new: Sequence[Mapping[str, Any]],
    flagged_ids: Iterable[str],
    *,
    id_field: str,
    noun: str,
    plural: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """A reviser's output with only the flagged items taken from it.

    Returns ``(items, notes)``: the pre-revision items ``old``, in their
    original order, with each item whose key (``item_keys`` on ``id_field``) is
    in ``flagged_ids`` replaced by the reviser's item with the same key (so the
    second item sharing an id is matched to the reviser's second item with
    that id, never collapsed into the first). Unflagged items the reviser
    changed, flagged items it dropped, and extra or duplicated ids are
    reverted/dropped; ``notes`` describes each intervention, naming items with
    ``noun`` / ``plural`` ([] = the reviser followed the rules).
    """
    flagged = {str(f) for f in flagged_ids}
    old_keys = item_keys(old, id_field)
    new_keys = item_keys(new, id_field)

    revised: dict[str, Mapping[str, Any]] = {}
    notes: list[str] = []
    for key, item in zip(new_keys, new, strict=True):
        if key in old_keys:
            revised[key] = item
        elif "#" in key[1:] and (base := key.split("#")[0]) in new_keys:
            notes.append(f"dropped a duplicate of {noun} {base}")
        else:
            notes.append(f"dropped {noun} {key}: not in the pre-revision {plural}")

    result: list[dict[str, Any]] = []
    for key, item in zip(old_keys, old, strict=True):
        if key in flagged and key in revised:
            result.append(dict(revised[key]))
            continue
        if key in flagged:
            notes.append(f"restored flagged {noun} {key}: missing from the revision")
        elif key not in revised:
            notes.append(f"restored {noun} {key}: missing from the revision")
        elif dict(revised[key]) != dict(item):
            notes.append(f"restored {noun} {key}: it was not flagged for revision")
        result.append(dict(item))
    if not notes and new_keys != old_keys:
        notes.append(f"restored the original {noun} order")
    return result, notes


def restore_unflagged(
    before: Any, after: Any, flagged_ids: Iterable[str]
) -> tuple[dict[str, Any], list[str]]:
    """The ad copy reviser's output with only the flagged copies taken from it.

    Returns ``({"ad_copies": [...]}, notes)`` (see ``restore_unflagged_items``,
    keyed by ``copy_keys``). With no usable pre-revision copies, ``after`` is
    returned as is.
    """
    old = parse_copies(before)
    new = parse_copies(after)
    if not old:
        return {"ad_copies": [dict(c) for c in new]}, []
    result, notes = restore_unflagged_items(
        old, new, flagged_ids, id_field="original_id", noun="copy", plural="copies"
    )
    return {"ad_copies": result}, notes
