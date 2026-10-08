"""Deterministic checks on the ad copy critic's final copies.

Pure logic (no ADK imports) behind ``creative_agent.agent.copy_gate``. Gating
policy (each needless revision costs a worker LLM call, latency and possibly a
user-visible warning, so false positives are kept low):

* **deterministic** issues — the rules the ``FinalAdCopyList`` output schema
  cannot express (product named, CTA, lengths, the brief's avoid terms) — gate
  a revision AND, if they survive it, are recorded as residual issues;
* **self_reported** issues — the critic's own failed ``brief_checks`` — gate a
  revision only for ``proposition`` and ``mandatories`` (the brief's hard
  contract, with no deterministic check), and are NEVER recorded as residual
  issues. Every other failed item is advisory: it stays in ``brief_checks``
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
failed ``reason_to_believe`` / ``trend_bridge`` self-reports
(``OFF_BRIEF_BRIEF_CHECKS``; still never recorded as residual issues).

Matching (product named, avoid terms) uses ``text_match``: accent-folded,
Unicode-aware, plural-insensitive (s / es / ies↔y), and brands written with
punctuation or digits ("AT&T", "M&M's", "7UP") also match their
punctuation-stripped forms. A product with nothing matchable ("GE") is never
flagged; naming the brand counts as naming the product (copy usually names
the brand — stricter than the image-prompt guard, which needs a brand anchor
in the product match). Avoid terms contained in the product, a non-negative
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

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from .brief_check import clean_term, parse_brief
from .text_match import PACKAGING_WORDS, contains_phrase, fold, same_word, words

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
# contract, which no deterministic check covers. The rest are advisory.
_GATING_ORDER = ("proposition", "mandatories")
GATING_BRIEF_CHECKS = frozenset(_GATING_ORDER)
# Items a deterministic check already covers; a self-reported failure of one is
# never listed (no double listing), even if it were made gating.
DETERMINISTIC_BRIEF_CHECKS = frozenset({"product", "avoid", "cta"})
# Rating strictness: a recurring "off-brief / wrong message" fail reason also
# makes the critic's failed message items gate (proposition already does):
# the reason to believe and the trend bridge. ``tone`` stays advisory (the
# off_brand_tone reason is guidance only).
OFF_BRIEF_BRIEF_CHECKS = frozenset({"reason_to_believe", "trend_bridge"})


def max_cta_words(strictness: Sequence[str] = ()) -> int:
    """The CTA word limit: ``STRICT_CTA_WORDS`` under ``weak_cta`` strictness."""
    return STRICT_CTA_WORDS if "weak_cta" in strictness else MAX_CTA_WORDS


def gating_brief_checks(strictness: Sequence[str] = ()) -> frozenset[str]:
    """The self-reported brief_checks items that gate (plus the message items
    under ``off_brief`` strictness)."""
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


def _names_product(copy_text: str, target_product: str, brand: str = "") -> bool:
    """The copy names the product: its full phrase, the ``brand`` (when given),
    ANY significant word, or a punctuation-stripped brand form (see
    ``_brand_forms``).

    Deliberately lenient (each false positive costs a revision call and a
    user-visible warning): "Lace up your skates" names "Rocket Skates", and
    "Only on Apple" names "iPhone 16 Pro" for the brand Apple — ad copy
    usually names the brand. (The image-prompt guard is stricter: there a
    partial product match must carry a brand token, see ``concept_guard``.)
    Accent-, case- and plural-insensitive (s / es / ies↔y).
    """
    if contains_phrase(copy_text, target_product):
        return True
    if brand.strip() and contains_phrase(copy_text, brand):
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


def _deterministic_issues(
    copy: Mapping[str, Any],
    *,
    target_product: str,
    avoid: list[str],
    brand: str,
    max_cta: int = MAX_CTA_WORDS,
) -> list[str]:
    issues: list[str] = []
    copy_text = " ".join(_text(copy.get(f)) for f in _COPY_FIELDS)

    if _has_matchable_tokens(target_product) and not _names_product(
        copy_text, target_product, brand
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

    for term in avoid:
        if contains_phrase(copy_text, term):
            issues.append(
                f"contains the avoided term '{term}': remove it or rephrase "
                "without it (the brief's avoid list)."
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


def _check_copy(
    copy: Mapping[str, Any],
    *,
    target_product: str,
    avoid: list[str],
    brand: str,
    strictness: Sequence[str] = (),
) -> list[CopyIssue]:
    deterministic = _deterministic_issues(
        copy,
        target_product=target_product,
        avoid=avoid,
        brand=brand,
        max_cta=max_cta_words(strictness),
    )
    self_reported = _self_reported_issues(copy, gating_brief_checks(strictness))
    return [CopyIssue("deterministic", text) for text in deterministic] + [
        CopyIssue("self_reported", text) for text in self_reported
    ]


def gate_copies(
    copies: Any,
    *,
    target_product: str,
    avoid: Iterable[str] | str = (),
    mandatories: Iterable[str] = (),
    trend: str = "",
    brand: str = "",
    strictness: Sequence[str] = (),
) -> dict[str, list[CopyIssue]]:
    """The copies' gating issues, keyed by ``copy_keys``.

    ``copies`` is the ``ad_copy_critique`` state value (see ``parse_copies``).
    Deterministic checks, per copy: the product is named (``target_product``,
    the ``brand``, ANY of its significant words, or a punctuation-stripped
    brand form;
    accent/case/plural-insensitive, see ``_names_product``) in the
    headline/body/caption/CTA — skipped when the product yields nothing to
    match; the CTA is non-empty and at most 8 words; the headline is at most 60
    characters; the social caption at most 2200; no term from ``avoid`` (the
    creative brief's avoid list; a string is split on newlines/commas/
    semicolons) appears as a whole word/phrase (entries over
    ``MAX_AVOID_TERM_WORDS`` words, and entries contained in the product name,
    a non-negative brief mandatory or a proper-name chunk of ``trend``, are
    skipped; see ``_avoid_terms``). Self-reported: each failed
    ``brief_checks`` item in ``GATING_BRIEF_CHECKS``. ``strictness`` (the
    run's ``rating_strictness`` flags; default none = the rules above) only
    tightens: ``weak_cta`` caps the CTA at ``STRICT_CTA_WORDS`` words and
    ``off_brief`` adds ``OFF_BRIEF_BRIEF_CHECKS`` to the gating items. Only
    copies with issues are returned ({} = clean). Never raises.
    """
    terms = _avoid_terms(
        avoid, product=target_product, mandatories=mandatories, trend=trend
    )
    parsed = parse_copies(copies)
    issues: dict[str, list[CopyIssue]] = {}
    for key, copy in zip(copy_keys(parsed), parsed, strict=True):
        if found := _check_copy(
            copy,
            target_product=target_product,
            avoid=terms,
            brand=brand,
            strictness=strictness,
        ):
            issues[key] = found
    return issues


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
