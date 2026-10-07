"""Deterministic guards on the final visual concepts (before rendering).

1. ``ensure_trend_and_product`` — last-line repair: every final image prompt
   names the trend motif, the product and the concept's ``brand_cue``, so the
   image model can't render a trend-less / product-less / unbranded ad.
   ``image_generation_prompt`` is the only text the image model sees, and prompt
   wording alone is a request, not a guarantee. This pure helper appends a short
   scene sentence when one is missing; ``callbacks.ensure_trend_and_product_callback``
   applies it to ``final_visual_concepts`` after the finalizer, the fixer (and
   interactive's reviser) write it. It cannot prove the rendered pixels show them.

2. ``concept_issues`` — the checks behind ``creative_agent.agent.concept_gate``
   (one bounded fix round by ``visual_concept_fixer``). Only rules a string
   check can decide are gated, and each heuristic is deliberately CONSERVATIVE
   (every flagged concept costs a worker LLM call, latency and possibly a
   user-visible warning, so a missed violation is cheaper than a false alarm):

   * quoted in-image text must match the paired copy's headline or CTA;
   * ``trend_motif`` must not be empty;
   * at most ``max_text_concepts`` concepts carry quoted in-image text;
   * at most one centred hero composition per set.

   Known limits: in-image text is recognised ONLY inside double quotes
   (straight or curly) — the prompts require quoting it, and single quotes are
   ambiguous with apostrophes — so unquoted text instructions are missed;
   meme/comic prompts are detected by keyword and fully exempted; the centred
   hero check is keyword based, ignores "off-centre" and sentences about text,
   type or logos, and cannot judge an unlabelled composition. Visual quality,
   brand-cue fit, avoid/fit_mode adherence are left to the LLM critic and the
   eval judge.

``restore_unflagged_concepts`` is the fixer's safety net (only flagged concepts
may change), mirroring ``copy_gate.restore_unflagged``.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .copy_gate import CopyIssue, item_keys, parse_copies, restore_unflagged_items

DEFAULT_MAX_TEXT_CONCEPTS = 2

# Straight "…" or curly “…” double quotes; the content must contain a letter.
_QUOTED = re.compile(r'"([^"\n]+)"|“([^”\n]+)”')
# A meme caption / comic speech bubble may be new short text (the guide's
# exception): such concepts are exempt from the quote match AND the text cap.
_MEME_OR_COMIC = re.compile(
    r"\b(?:memes?|captions?|speech[- ]bubbles?|comics?)\b", re.IGNORECASE
)
# A centred hero composition ("off-centre" excluded).
_CENTRED = re.compile(
    r"(?<!off-)(?<!off )\b(?:cent(?:er|r)ed|cent(?:er|re)[- ]framed|"
    r"symmetrical hero|dead[- ]cent(?:er|re))\b",
    re.IGNORECASE,
)
# A sentence about text placement ("the headline is centred at the bottom") is
# not a hero composition.
_TEXT_WORDS = re.compile(
    r"\b(?:text|headline|type|typography|lettering|font|words?|title|"
    r"caption|cta|call[- ]to[- ]action|logo|wordmark)\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[^.!?;]+")


def ensure_trend_and_product(
    concepts: list[dict[str, Any]], target_product: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return repaired copies of `concepts` plus one warning per repair/miss.

    Appends the trend motif, the product and the concept's ``brand_cue`` when
    the prompt does not contain them (case-insensitive containment; lenient).
    """
    out: list[dict[str, Any]] = []
    warns: list[str] = []
    product = (target_product or "").strip()
    for concept in concepts:
        c = copy.deepcopy(concept)
        prompt = str(c.get("image_generation_prompt") or "").rstrip()
        low = prompt.lower()
        motif = str(c.get("trend_motif") or "").strip()
        cue = str(c.get("brand_cue") or "").strip()
        name = c.get("concept_name", "?")
        if not motif:
            warns.append(f"{name}: empty trend_motif")
        elif motif.lower() not in low:
            prompt += f" The scene visibly includes {motif}."
            warns.append(f"{name}: trend_motif missing from prompt, appended")
        if product and product.lower() not in low:
            prompt += f" The {product} is clearly visible and recognizable."
            warns.append(f"{name}: product missing from prompt, appended")
        if cue and cue.lower() not in low:
            prompt += f" The scene features {cue}."
            warns.append(f"{name}: brand_cue missing from prompt, appended")
        c["image_generation_prompt"] = prompt
        out.append(c)
    return out, warns


def parse_concepts(concepts: Any) -> list[Mapping[str, Any]]:
    """The concepts as a list of mappings (never raises).

    Accepts a list of concept dicts, the ``VisualConceptFinalList`` dict
    (``{"visual_concepts": [...]}``) or a JSON string of either; anything else
    (or a malformed entry) is dropped.
    """
    if isinstance(concepts, str):
        try:
            concepts = json.loads(concepts)
        except ValueError:
            return []
    if isinstance(concepts, Mapping):
        concepts = concepts.get("visual_concepts")
    if not isinstance(concepts, list):
        return []
    return [c for c in concepts if isinstance(c, Mapping)]


def concept_keys(concepts: Sequence[Mapping[str, Any]]) -> list[str]:
    """Each concept's issue key: ``item_keys`` on ``ad_copy_id``."""
    return item_keys(concepts, "ad_copy_id")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalise(text: str) -> str:
    """Lower-case alphanumeric words joined by single spaces (no punctuation)."""
    return " ".join(re.findall(r"[a-z0-9]+", text.replace("’", "'").lower()))


def quoted_texts(prompt: str) -> list[str]:
    """The double-quoted segments of ``prompt`` that contain a letter."""
    found = (m.group(1) or m.group(2) for m in _QUOTED.finditer(prompt))
    return [q.strip() for q in found if re.search(r"[A-Za-z]", q)]


def is_meme_or_comic(prompt: str) -> bool:
    """The prompt describes a meme caption / comic speech bubble (exempt)."""
    return _MEME_OR_COMIC.search(prompt) is not None


def is_centred_hero(prompt: str) -> bool:
    """A sentence of ``prompt`` (outside quotes) describes a centred hero."""
    unquoted = _QUOTED.sub(" ", prompt)
    return any(
        _CENTRED.search(sentence) and not _TEXT_WORDS.search(sentence)
        for sentence in _SENTENCE.findall(unquoted)
    )


def _matches(quote: str, allowed: Iterable[str]) -> bool:
    """``quote`` equals, contains or is contained in an allowed text (normalised)."""
    q = _normalise(quote)
    if not q:
        return True
    for text in allowed:
        t = _normalise(text)
        if t and (q in t or t in q):
            return True
    return False


def _paired_copy_texts(
    concept: Mapping[str, Any], copies_by_id: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """The paired copy's headline/CTA; the concept's own when unpaired."""
    source = copies_by_id.get(str(concept.get("ad_copy_id")), concept)
    return [
        t
        for t in (_text(source.get("headline")), _text(source.get("call_to_action")))
        if t
    ]


def concept_issues(
    concepts: Any,
    ad_copies: Any,
    *,
    max_text_concepts: int = DEFAULT_MAX_TEXT_CONCEPTS,
) -> dict[str, list[CopyIssue]]:
    """The concepts' deterministic issues, keyed by ``concept_keys``.

    ``concepts`` is the ``final_visual_concepts`` state value and ``ad_copies``
    the ``ad_copy_critique`` value (see ``parse_concepts`` / ``parse_copies``).
    Per concept (meme/comic prompts exempt from the first): every quoted text
    in ``image_generation_prompt`` matches the paired copy's (``original_id`` ==
    ``ad_copy_id``) headline or call to action — case, punctuation and
    whitespace insensitive, substring either way; an unpaired concept is
    checked against its own headline/CTA fields, and skipped when it has
    neither; ``trend_motif`` is non-empty. Per set: the concepts with quoted
    in-image text (meme/comic exempt) beyond the first ``max_text_concepts``,
    and every centred hero after the first, are flagged. Only concepts with
    issues are returned ({} = clean). Never raises.
    """
    parsed = parse_concepts(concepts)
    copies = parse_copies(ad_copies)
    copies_by_id: dict[str, Mapping[str, Any]] = {}
    for ad in copies:
        copies_by_id.setdefault(str(ad.get("original_id")), ad)

    issues: dict[str, list[CopyIssue]] = {}
    text_concepts = 0
    centred = 0
    for key, concept in zip(concept_keys(parsed), parsed, strict=True):
        found: list[str] = []
        prompt = _text(concept.get("image_generation_prompt"))
        exempt = is_meme_or_comic(prompt)
        quotes = [] if exempt else quoted_texts(prompt)

        allowed = _paired_copy_texts(concept, copies_by_id)
        if allowed:
            for quote in quotes:
                if not _matches(quote, allowed):
                    choices = " or ".join(f'"{t}"' for t in allowed)
                    found.append(
                        f'in-image text "{quote}" is not the paired ad copy\'s '
                        f"headline or call to action: quote {choices} exactly, "
                        "or remove the quoted text."
                    )

        if not _text(concept.get("trend_motif")):
            found.append(
                "trend_motif is empty: add a concrete, trend-specific visual "
                "element and write it verbatim into image_generation_prompt."
            )

        if quotes:
            text_concepts += 1
            if text_concepts > max_text_concepts:
                found.append(
                    f"in-image text appears in more than {max_text_concepts} "
                    "concepts: remove the quoted text from this concept and leave "
                    "clean negative space instead."
                )

        if is_centred_hero(prompt):
            centred += 1
            if centred > 1:
                found.append(
                    "more than one centred hero in the set: re-compose this "
                    "concept off-centre (rule-of-thirds, a small subject in a wide "
                    "environment, or an extreme close-up detail)."
                )

        if found:
            issues[key] = [CopyIssue("deterministic", text) for text in found]
    return issues


def _concept_label(concepts: Sequence[Mapping[str, Any]], key: str) -> str:
    for concept_key, concept in zip(concept_keys(concepts), concepts, strict=True):
        if concept_key == key:
            name = _text(concept.get("concept_name"))
            return f'Concept {key} ("{name}")' if name else f"Concept {key}"
    return f"Concept {key}"


def format_concept_issues(concepts: Any, issues: Mapping[str, Sequence[object]]) -> str:
    """The issues as a Markdown list grouped per concept (the fixer's input)."""
    parsed = parse_concepts(concepts)
    lines: list[str] = []
    for key, items in issues.items():
        lines.append(f"- **{_concept_label(parsed, key)}:**")
        lines.extend(f"  - {item}" for item in items)
    return "\n".join(lines)


def flatten_concept_issues(
    concepts: Any, issues: Mapping[str, Sequence[object]]
) -> list[str]:
    """One string per issue, prefixed with its concept (the residual record)."""
    parsed = parse_concepts(concepts)
    return [
        f"{_concept_label(parsed, key)}: {item}"
        for key, items in issues.items()
        for item in items
    ]


def restore_unflagged_concepts(
    before: Any, after: Any, flagged_ids: Iterable[str]
) -> tuple[dict[str, Any], list[str]]:
    """The fixer's output with only the flagged concepts taken from it.

    Returns ``({"visual_concepts": [...]}, notes)`` (see
    ``copy_gate.restore_unflagged_items``, keyed by ``concept_keys``). With no
    usable pre-revision concepts, ``after`` is returned as is.
    """
    old = parse_concepts(before)
    new = parse_concepts(after)
    if not old:
        return {"visual_concepts": [dict(c) for c in new]}, []
    result, notes = restore_unflagged_items(
        old, new, flagged_ids, id_field="ad_copy_id", noun="concept", plural="concepts"
    )
    return {"visual_concepts": result}, notes
