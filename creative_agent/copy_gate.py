"""Deterministic checks on the ad copy critic's final copies.

Pure logic (no ADK imports) behind ``creative_agent.agent.copy_gate``: the rules
the ``FinalAdCopyList`` output schema cannot express, plus the critic's own
failed brief-checklist items. Each issue string is specific and actionable
because the gate feeds them verbatim to ``ad_copy_reviser`` (as
``ad_copy_issues``) and records any that survive the bounded revision as
``ad_copy_critique__issues`` (surfaced by
``agent_common.observability.collect_degradation_warnings``).

``restore_unflagged`` is the reviser's safety net: the reviser may only rewrite
the copies the gate flagged, so every other copy (and any copy it dropped or
duplicated) is put back to its pre-revision value.
"""

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .brief_check import parse_brief

MAX_HEADLINE_CHARS = 60
MAX_CAPTION_CHARS = 2200
MAX_CTA_WORDS = 8

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
# Every copy field the product may be named in (the CTA counts too).
_COPY_FIELDS = ("headline", "body_text", "social_caption", "call_to_action")
_POSSESSIVE = re.compile(r"['\u2019]s\b", re.IGNORECASE)


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


def copy_keys(copies: list[Mapping[str, Any]]) -> list[str]:
    """Each copy's issue key, unique within ``copies``.

    ``str(original_id)``; ``#<index>`` for a copy without one; and
    ``<original_id>#<n>`` for the n-th (n >= 2) copy repeating an id, so a
    model that duplicated an id never gets two copies collapsed into one by
    the gate or by ``restore_unflagged``.
    """
    keys: list[str] = []
    seen: dict[str, int] = {}
    for index, copy in enumerate(copies):
        original_id = copy.get("original_id")
        if original_id is None or isinstance(original_id, bool):
            keys.append(f"#{index}")
            continue
        base = str(original_id)
        seen[base] = seen.get(base, 0) + 1
        keys.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return keys


def brief_avoid(brief: Mapping[str, Any] | str | None) -> list[str]:
    """The structured brief's ``avoid`` list (brief dict or its JSON string)."""
    data = parse_brief(brief)
    if data is None:
        return []
    avoid = data.get("avoid")
    if not isinstance(avoid, list):
        return []
    return [a.strip() for a in avoid if isinstance(a, str) and a.strip()]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _words(text: str) -> list[str]:
    """Lower-cased words of ``text``, with possessive 's / ’s stripped."""
    return re.findall(r"[a-z0-9]+", _POSSESSIVE.sub("", text).lower())


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """``phrase`` occurs in ``haystack`` as whole words (case-insensitive)."""
    words = _words(phrase)
    if not words:
        return False
    pattern = r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(map(re.escape, words))
    return re.search(pattern + r"(?![a-z0-9])", haystack.lower()) is not None


def product_words(target_product: str) -> list[str]:
    """The product's significant words (lower-cased, possessives stripped).

    Skips stopwords, pure numbers (model numbers, years) and words under 3
    characters: "PRS SE Custom 24" yields ["prs", "custom"]. Brand words are
    kept: when the product phrase contains the brand ("Powerball tickets"),
    naming the brand names the product.
    """
    return [
        word
        for word in _words(target_product)
        if len(word) >= _MIN_TOKEN_CHARS
        and word not in _STOPWORDS
        and not word.isdigit()
    ]


def _same_word(a: str, b: str) -> bool:
    """Equal, or singular/plural of each other (a trailing "s" or "es")."""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return long_ in (f"{short}s", f"{short}es")


def _names_product(copy_text: str, target_product: str) -> bool:
    """The copy names the product: its full phrase, or ANY significant word.

    Deliberately lenient (each false positive costs a revision call and a
    user-visible warning): "Lace up your skates" names "Rocket Skates".
    """
    if _contains_phrase(copy_text, target_product):
        return True
    copy_words = set(_words(copy_text))
    return any(
        _same_word(word, other)
        for word in product_words(target_product)
        for other in copy_words
    )


def _avoid_terms(avoid: Iterable[str] | str) -> list[str]:
    if isinstance(avoid, str):
        avoid = re.split(r"[\n;,]", avoid)
    return [t.strip() for t in avoid if isinstance(t, str) and t.strip()]


def _check_copy(
    copy: Mapping[str, Any],
    *,
    target_product: str,
    avoid: list[str],
) -> list[str]:
    issues: list[str] = []
    copy_text = " ".join(_text(copy.get(f)) for f in _COPY_FIELDS)

    if target_product.strip() and not _names_product(copy_text, target_product):
        issues.append(
            f"product not named: mention '{target_product.strip()}' in the "
            "headline, body text, social caption or call to action."
        )

    cta = _text(copy.get("call_to_action"))
    if not cta:
        issues.append(
            "call_to_action is empty: add a specific CTA that starts with an "
            f"action verb (at most {MAX_CTA_WORDS} words)."
        )
    elif (n := len(cta.split())) > MAX_CTA_WORDS:
        issues.append(
            f"call_to_action has {n} words ('{cta}'): cut it to at most "
            f"{MAX_CTA_WORDS} words, starting with an action verb."
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

    for term in avoid:
        if _contains_phrase(copy_text, term):
            issues.append(
                f"contains the avoided term '{term}': remove it or rephrase "
                "without it (the brief's avoid list)."
            )

    checks = copy.get("brief_checks")
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, Mapping) or check.get("passed") is not False:
            continue
        item = _text(check.get("item")) or "(unnamed item)"
        note = _text(check.get("note"))
        issues.append(
            f"brief check failed: {item} — {note}"
            if note
            else (f"brief check failed: {item}")
        )
    return issues


def gate_copies(
    copies: Any,
    *,
    target_product: str,
    avoid: Iterable[str] | str = (),
) -> dict[str, list[str]]:
    """The copies' rule violations, keyed by ``copy_keys``.

    ``copies`` is the ``ad_copy_critique`` state value (see ``parse_copies``).
    Checks, per copy: the product is named (``target_product`` or ANY of its
    significant words, plural-insensitive, see ``product_words``) in the
    headline/body/caption/CTA; the
    CTA is non-empty and at most 8 words; the headline is at most 60 characters;
    the social caption at most 2200; no term from ``avoid`` (the creative
    brief's avoid list; a string is split on newlines/commas/semicolons) appears
    as a whole word/phrase; and every ``brief_checks`` item the critic marked
    ``passed: false`` becomes an issue. Only copies with issues are returned
    ({} = clean). Never raises.
    """
    terms = _avoid_terms(avoid)
    parsed = parse_copies(copies)
    issues: dict[str, list[str]] = {}
    for key, copy in zip(copy_keys(parsed), parsed, strict=True):
        if found := _check_copy(copy, target_product=target_product, avoid=terms):
            issues[key] = found
    return issues


def _copy_label(copies: list[Mapping[str, Any]], key: str) -> str:
    for copy_id, copy in zip(copy_keys(copies), copies, strict=True):
        if copy_id == key:
            headline = _text(copy.get("headline"))
            return f'Copy {key} ("{headline}")' if headline else f"Copy {key}"
    return f"Copy {key}"


def format_copy_issues(copies: Any, issues: Mapping[str, list[str]]) -> str:
    """The issues as a Markdown list grouped per copy (the reviser's input)."""
    parsed = parse_copies(copies)
    lines: list[str] = []
    for key, items in issues.items():
        lines.append(f"- **{_copy_label(parsed, key)}:**")
        lines.extend(f"  - {item}" for item in items)
    return "\n".join(lines)


def flatten_copy_issues(copies: Any, issues: Mapping[str, list[str]]) -> list[str]:
    """One string per issue, prefixed with its copy (the residual-issue record)."""
    parsed = parse_copies(copies)
    return [
        f"{_copy_label(parsed, key)}: {item}"
        for key, items in issues.items()
        for item in items
    ]


def restore_unflagged(
    before: Any, after: Any, flagged_ids: Iterable[str]
) -> tuple[dict[str, Any], list[str]]:
    """The reviser's output with only the flagged copies taken from it.

    Returns ``({"ad_copies": [...]}, notes)``: the pre-revision copies, in their
    original order, with each copy whose key (``copy_keys``) is in
    ``flagged_ids`` replaced by the reviser's copy with the same key (so the
    second copy sharing an id is matched to the reviser's second copy with that
    id, never collapsed into the first). Unflagged copies the reviser changed, flagged copies it
    dropped, and extra or duplicated ids are reverted/dropped; ``notes``
    describes each intervention ([] = the reviser followed the rules). With no
    usable pre-revision copies, ``after`` is returned as is.
    """
    old = parse_copies(before)
    new = parse_copies(after)
    if not old:
        return {"ad_copies": [dict(c) for c in new]}, []
    flagged = {str(f) for f in flagged_ids}
    old_keys = copy_keys(old)
    new_keys = copy_keys(new)

    revised: dict[str, Mapping[str, Any]] = {}
    notes: list[str] = []
    for key, copy in zip(new_keys, new, strict=True):
        if key in old_keys:
            revised[key] = copy
        elif "#" in key[1:] and (base := key.split("#")[0]) in new_keys:
            notes.append(f"dropped a duplicate of copy {base}")
        else:
            notes.append(f"dropped copy {key}: not in the pre-revision copies")

    result: list[dict[str, Any]] = []
    for key, copy in zip(old_keys, old, strict=True):
        if key in flagged and key in revised:
            result.append(dict(revised[key]))
            continue
        if key in flagged:
            notes.append(f"restored flagged copy {key}: missing from the revision")
        elif key not in revised:
            notes.append(f"restored copy {key}: missing from the revision")
        elif dict(revised[key]) != dict(copy):
            notes.append(f"restored copy {key}: it was not flagged for revision")
        result.append(dict(copy))
    if not notes and new_keys != old_keys:
        notes.append("restored the original copy order")
    return {"ad_copies": result}, notes
