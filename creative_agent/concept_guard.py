"""Deterministic guards on the final visual concepts (before rendering).

1. ``ensure_trend_and_product`` — last-line repair: every final image prompt
   names the trend motif, the product and the concept's ``brand_cue``, so the
   image model can't render a trend-less / product-less / unbranded ad.
   ``image_generation_prompt`` is the only text the image model sees, and prompt
   wording alone is a request, not a guarantee. This pure helper appends a short
   scene sentence when one is missing; ``callbacks.ensure_trend_and_product_callback``
   applies it to ``final_visual_concepts`` after the finalizer, the fixer (and
   interactive's reviser) write it. It cannot prove the rendered pixels show them.

   "Missing" uses ``text_match.mentions`` (token overlap), not a literal
   substring, for all three: the finalizer routinely paraphrases ("a PRS SE
   guitar" for "PRS SE CE24 electric guitar", "friendship-bracelet stack under
   Eras Tour lights" for "Eras Tour friendship bracelets", "cookie" for
   "cookies", "PRS … bird-shaped fretboard inlays" for the brand cue "PRS bird
   inlays"), and a redundant appended sentence both clutters the prompt and
   raises a false warning. A phrase counts as present in full or when at least
   60% of its content tokens appear (a bare "jacket" for "Patagonia Nano Puff
   Jacket" is 1 of 4: still appended). The product and brand-cue matches are
   brand-anchored: when the phrase contains the brand, a partial match must
   include a brand token ("an iced coffee" is not "Starbucks iced coffee") —
   unlike the copy gate, where naming the brand alone counts as naming the
   product (copy usually names the brand; an image prompt must show the
   product).

   Intangible products (`is_intangible`: subscriptions, apps, services, plans,
   insurance, internet…) cannot be "clearly visible and recognizable"; for them
   the guard skips the append when the brand is already mentioned, and otherwise
   appends a depictable cue (`INTANGIBLE_PRODUCT_LINE`) instead.

   Rating strictness (opt-in rating learning, ``rating_strictness``; default
   none) only adds sentences: ``trend_unclear`` appends the motif unless the
   prompt holds it verbatim, ``product_not_visible`` appends
   ``PROMINENT_PRODUCT_LINE`` (tangible products only); both idempotent.

1b. ``enforce_person_casting`` — runs first in the same callback: clears
   ``casts_person_reference`` when no consented person reference is available,
   the style is not person-safe (``config.person_safe_styles``), the prompt has
   no human subject, or beyond ``config.max_cast_concepts`` casts (first N in
   concept order kept). The render step re-applies it before attaching the photo.

2. ``concept_issues`` — the checks behind ``creative_agent.agent.concept_gate``
   (one bounded fix round by ``visual_concept_fixer``). Only rules a string
   check can decide are gated, and each heuristic is deliberately CONSERVATIVE
   (every flagged concept costs a worker LLM call, latency and possibly a
   user-visible warning, so a missed violation is cheaper than a false alarm):

   * quoted in-image text must match the paired copy's headline or CTA
     (quotes of the brand, the product or the concept's ``brand_cue`` — a
     logo, a product name — are always allowed);
   * ``trend_motif`` must not be empty;
   * at most ``max_text_concepts`` concepts carry quoted headline/CTA in-image
     text (or unchecked text on an unpaired concept) — brand/product quotes
     and concepts already flagged for a mismatch never count. When the
     legitimate text concepts already fill the cap, a mismatch issue asks to
     remove the quoted text rather than offering to quote the copy (which
     would only trade the mismatch for a cap overflow);
   * at most one centred hero composition per set.

   ``text_problem`` rating strictness lowers the text cap to
   ``STRICT_MAX_TEXT_CONCEPTS`` (1).

   In-image text heuristic (``in_image_quotes``): a double-quoted span
   (straight or curly; it must contain a letter) is in-image text ONLY when a
   text cue word (reading/reads/says/text/headline/tagline/sign/caption/
   lettering/written/words/title/label/slogan/banner/poster/font/typography/
   overlay/spells/printed/emblazoned/stencilled/quote/phrase/wording/
   inscribed …, see ``_TEXT_CUE``) appears within the ``_CUE_WINDOW_WORDS`` (6)
   words before it (after any earlier quote); the often-descriptive cues
   painted/neon/bearing/displays/displaying (``_ADJACENT_ONLY_CUES``) count
   only as the word directly before the quote. A quote joined to an in-image one inherits it: only punctuation /
   "and"/"or"/"then"/"plus" between them, or up to 4 short words ending in one
   of those connectors (``reads "Go" in bold, then "Now"``). Idioms are not
   cues: a cue followed by "of" (the "X of" idiom — "sign of the times",
   "title of the song", "banner of light", "words of encouragement"), by
   "style"/"styled" ("poster style") or by "cannot/can/could/fail" ("words
   cannot capture"), a hyphenated compound ("label-free", "title-card") and
   "vibe/mood/feel … reads/says".
   Any other quoted span — ``bathed in "golden hour" light``, a quoted style
   name — is descriptive and ignored entirely (neither a mismatch nor counted
   toward the cap). Known limits: unquoted text instructions, single-quoted
   text (ambiguous with apostrophes) and quoted text whose cue comes only
   after it are missed. Meme/comic concepts are fully exempt: by
   ``visual_style`` containing "meme" or a meme/comic palette family
   (``EXEMPT_STYLE_FAMILIES``), else by the narrow prompt phrases "meme
   caption" / "speech bubble|balloon" / "thought bubble" / "comic panel" /
   "Impact font", or "top|bottom text" when the prompt also says "meme" or
   "Impact" (never bare "caption"/"meme", which the guide's negative-space
   wording uses, nor "headline as bottom text" alone). The centred hero check is keyword
   based: "dead centre" / "centre-framed" / "symmetrical hero" always count; a
   bare "centred" needs a subject/composition word in the sentence. It ignores
   "off-centre", "centred between", "camera centred on" followed in the
   sentence by a wide/crowd word (crowd/wide/landscape/scene/room/street/
   stadium/audience), "centred in/on/at the lower/upper/left/right/top/bottom
   third", negations ("not centred", "avoid centring/a
   centred …"), "small … in a wide" framing and sentences about text, type or
   logos; it cannot judge an unlabelled composition. Visual
   quality, brand-cue fit, avoid/fit_mode adherence are left to the LLM critic
   and the eval judge.

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
from .text_match import content_tokens, mentions, same_word

DEFAULT_MAX_TEXT_CONCEPTS = 2
# Rating strictness (``rating_strictness``, opt-in rating learning): a
# recurring "in-image text problems" fail reason caps text concepts at 1.
STRICT_MAX_TEXT_CONCEPTS = 1

# Straight "…" or curly “…” double quotes; the content must contain a letter.
_QUOTED = re.compile(r'"([^"\n]+)"|“([^”\n]+)”')
# A word that marks the following quoted span as text rendered in the image.
_TEXT_CUE = re.compile(
    r"^(?:reading|reads|read|says|saying|text|headline|tagline|sign|signage|"
    r"caption|lettering|letters|written|words|title|label|slogan|banner|poster|"
    r"font|typography|typeset|overlay|spells|spelling|spelled|printed|"
    r"emblazoned|stencilled|stenciled|quote|quoted|quotes|phrase|wording|"
    r"inscription|inscribed|displays|displaying|bearing|painted|neon)$",
    re.IGNORECASE,
)
_CUE_WINDOW_WORDS = 6
# Cue words that are just as often descriptive ("painted in warm "golden hour"
# tones", "bathed in neon, a "cyberpunk" vibe", "bearing a "rock star"
# swagger", "displays a "lived-in" feel"): they count only directly before the
# quote ("glowing neon "Open"", "a billboard displaying "Stay Dry"").
_ADJACENT_ONLY_CUES = frozenset(
    {"painted", "neon", "bearing", "displays", "displaying"}
)
# Words (a hyphenated compound is ONE word, so "label-free" / "title-card" are
# not cues) used for the cue window.
_WORD = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")
# A cue followed by one of these is an idiom, not text: "sign of the times",
# "title of the song", "banner of light", "words of encouragement", "poster
# style", "words cannot capture".
_NOT_CUE_BEFORE = frozenset(
    {"of", "style", "styled", "cannot", "can", "could", "fail", "fails"}
)
# "the vibe reads …" / "the mood says …" describe a feeling, not text.
_SPEECH_CUES = frozenset({"reading", "reads", "read", "says", "saying"})
_MOOD_WORDS = frozenset(
    {"vibe", "vibes", "mood", "feel", "feeling", "tone", "energy", "atmosphere"}
)
# Only punctuation / "and" / "or" between two quotes: the second inherits the
# first's in-image status (``reads "A" and "B"``).
_QUOTE_JOINER = re.compile(
    r"^[\s,;:&/+\-–—]*(?:(?:and|or|then|plus)\b[\s,;:&/+\-–—]*)*$", re.IGNORECASE
)
# Or a few short words ending in a connector (``reads "Go" in bold, then
# "Now"``); "beside a" / "and a" (no trailing connector) never join.
_QUOTE_JOINER_TAIL = re.compile(
    r"\b(?:and|or|then|plus)[\s,;:&/+\-–—]*$", re.IGNORECASE
)
_JOIN_MAX_WORDS = 4
_JOIN_MAX_WORD_LEN = 10
# A meme caption / comic speech bubble may be new short text (the guide's
# exception): such concepts are exempt from the quote match AND the text cap.
# Primary signal: the concept's visual_style contains "meme" or names one of
# these palette families (creative_agent.style_shortlist.STYLE_GROUPS);
# fallback: the narrow prompt phrases below (never bare "caption"/"meme").
EXEMPT_STYLE_FAMILIES: tuple[str, ...] = ("Meme aesthetic", "Comic panel")
_MEME_OR_COMIC = re.compile(
    r"\b(?:meme[- ]captions?|speech[- ](?:bubbles?|balloons?)|thought[- ]bubbles?|"
    r"comic[- ]panels?|impact[- ]font)\b",
    re.IGNORECASE,
)
# "top/bottom text" is meme layout only when the prompt also says "meme" or
# "Impact" (the font); "headline as bottom text" alone is a plain ad layout.
_TOP_BOTTOM_TEXT = re.compile(r"\b(?:top|bottom)[- ]text\b", re.IGNORECASE)
_MEME_WORD = re.compile(r"\b(?i:memes?)\b|\bImpact\b")
# Unambiguous centred-hero phrases.
_STRONG_CENTRED = re.compile(
    r"(?<!off-)(?<!off )\b(?:cent(?:er|re)[- ]framed|symmetrical hero|"
    r"dead[- ]cent(?:er|re))\b",
    re.IGNORECASE,
)
# A bare "centred"/"centered" ("off-centre" and "centred between …" excluded)
# — counts only next to a subject/composition word.
_BARE_CENTRED = re.compile(
    r"(?<!off-)(?<!off )\bcent(?:er|r)ed\b(?!\s+between\b)", re.IGNORECASE
)
# Placements removed from a sentence before the bare check: "camera centred on"
# a wide/crowd subject ("camera centered on the crowd, …") and "centred in the
# lower third" (the subject sits on a third line, not in the middle).
_NOT_HERO_CENTRING = re.compile(
    r"\bcamera\s+(?:is\s+)?cent(?:er|r)ed\s+on\b(?=[^.!?;]*\b(?:crowds?|wide|"
    r"landscapes?|scenes?|rooms?|streets?|stadiums?|audiences?)\b)"
    r"|\bcent(?:er|r)ed\s+(?:in|on|at)\s+the\s+"
    r"(?:lower|upper|left|right|top|bottom)\s+third\b",
    re.IGNORECASE,
)
_HERO_CONTEXT = re.compile(
    r"\b(?:subject|product|hero|figure|person|character|object|composition|"
    r"framing|framed|frame|shot|portrait|symmetr\w*)\b",
    re.IGNORECASE,
)
# A sentence about text placement ("the headline is centred at the bottom") is
# not a hero composition.
_TEXT_WORDS = re.compile(
    r"\b(?:text|headline|type|typography|lettering|font|words?|title|"
    r"caption|cta|call[- ]to[- ]action|logo|wordmark)\b",
    re.IGNORECASE,
)
# "not centred", "never centered", "avoid centring", "avoid a centred hero".
_NEGATED_CENTRE = re.compile(
    r"\b(?:not|never|avoid(?:s|ing)?)\s+(?:\w+\s+){0,2}?cent(?:er|r)",
    re.IGNORECASE,
)
# "a small subject in a wide environment" — the guide's off-hero framing.
_SMALL_IN_WIDE = re.compile(r"\bsmall\b[^.!?;]*\bin an?\b[^.!?;]*\bwide\b", re.I)
_SENTENCE = re.compile(r"[^.!?;]+")

TANGIBLE_PRODUCT_LINE = " The {product} is clearly visible and recognizable."
INTANGIBLE_PRODUCT_LINE = (
    " The {product} is suggested through a branded app screen or logo in the scene."
)
MOTIF_LINE = " The scene visibly includes {motif}."
BRAND_CUE_LINE = " The scene features {cue}."
# Rating strictness: a recurring "product hard to see" fail reason.
PROMINENT_PRODUCT_LINE = " The product is large and in the foreground."

# Content tokens marking a product the camera cannot show as an object. Kept
# deliberately to unambiguous service words: "card" (a credit card can be
# shown), "premium" or "pass" (also physical product names) are not here.
INTANGIBLE_WORDS = frozenset(
    {
        "account",
        "app",
        "application",
        "banking",
        "broadband",
        "insurance",
        "internet",
        "loan",
        "membership",
        "mortgage",
        "plan",
        "platform",
        "service",
        "software",
        "streaming",
        "subscription",
        "wifi",
    }
)


def is_intangible(target_product: str) -> bool:
    """The product names a service/subscription, not a depictable object."""
    return any(
        same_word(token, word)
        for token in content_tokens(target_product)
        for word in INTANGIBLE_WORDS
    )


def _verbatim(prompt: str, phrase: str) -> bool:
    return " ".join(phrase.split()).casefold() in " ".join(prompt.split()).casefold()


def ensure_trend_and_product(
    concepts: list[dict[str, Any]],
    target_product: str,
    *,
    brand: str = "",
    strictness: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return repaired copies of `concepts` plus one warning per repair/miss.

    Appends the trend motif, the product and the concept's ``brand_cue`` when
    the prompt does not mention them (``text_match.mentions``; the product and
    the brand cue brand-anchored on ``brand``; intangible products per the
    module doc).

    ``strictness`` (the run's ``rating_strictness`` flags; default none = the
    rules above) only adds sentences: ``trend_unclear`` appends the motif
    unless the prompt contains it verbatim (case/whitespace-insensitive), and
    ``product_not_visible`` appends ``PROMINENT_PRODUCT_LINE`` for a tangible
    product. Both are idempotent, so re-guarding a prompt never duplicates them.
    """
    out: list[dict[str, Any]] = []
    warns: list[str] = []
    product = (target_product or "").strip()
    brand = (brand or "").strip()
    intangible = bool(product) and is_intangible(product)
    strict_motif = "trend_unclear" in strictness
    prominent = "product_not_visible" in strictness and bool(product) and not intangible
    for concept in concepts:
        c = copy.deepcopy(concept)
        prompt = str(c.get("image_generation_prompt") or "").rstrip()
        original = prompt
        motif = str(c.get("trend_motif") or "").strip()
        cue = str(c.get("brand_cue") or "").strip()
        name = c.get("concept_name", "?")
        if not motif:
            warns.append(f"{name}: empty trend_motif")
        elif not mentions(original, motif):
            prompt += MOTIF_LINE.format(motif=motif)
            warns.append(f"{name}: trend_motif missing from prompt, appended")
        elif strict_motif and not _verbatim(original, motif):
            prompt += MOTIF_LINE.format(motif=motif)
            warns.append(
                f"{name}: trend_motif not verbatim in prompt, appended "
                "(rating strictness)"
            )
        if product and not mentions(original, product, brand=brand):
            if not intangible:
                prompt += TANGIBLE_PRODUCT_LINE.format(product=product)
                warns.append(f"{name}: product missing from prompt, appended")
            elif not (brand and mentions(original, brand)):
                prompt += INTANGIBLE_PRODUCT_LINE.format(product=product)
                warns.append(f"{name}: product missing from prompt, appended")
        # Checked against the repaired prompt: a cue the product line just
        # appended (cue == product) is not appended twice.
        if cue and not mentions(prompt, cue, brand=brand):
            prompt += BRAND_CUE_LINE.format(cue=cue)
            warns.append(f"{name}: brand_cue missing from prompt, appended")
        if prominent and not _verbatim(prompt, PROMINENT_PRODUCT_LINE):
            prompt += PROMINENT_PRODUCT_LINE
            warns.append(
                f"{name}: product prominence line appended (rating strictness)"
            )
        c["image_generation_prompt"] = prompt
        out.append(c)
    return out, warns


# --- Person casting ---------------------------------------------------------------

# How a cast concept's prompt names its hero (PERSON_CASTING_RULES); the render
# step attaches the person photo as "Reference image N (person)".
PERSON_HERO_PHRASE = "the person in the person reference image"
PERSON_HERO_LINE = " The hero is the person in the person reference image."
_PERSON_HERO_RE = re.compile(re.escape(PERSON_HERO_PHRASE), re.IGNORECASE)
# A single human subject named in the prompt (conservative: any of these words).
_HUMAN_CUE = re.compile(
    r"\b(?:person|man|woman|men|women|guy|girl|boy|lady|hero|heroine|model|"
    r"athlete|musician|player|guitarist|drummer|singer|dancer|runner|skater|"
    r"cyclist|rider|surfer|climber|chef|cook|barista|driver|traveller|traveler|"
    r"hiker|student|parent|mother|father|mum|mom|dad|customer|shopper|fan|"
    r"gamer|worker|nurse|teacher|portrait|selfie)s?\b",
    re.IGNORECASE,
)


def neutralise_person_prompt(prompt: str) -> str:
    """``prompt`` with the person-reference wording turned into "a person" (for
    a concept rendered without the photo)."""
    return _PERSON_HERO_RE.sub("a person", prompt or "")


def enforce_person_casting(
    concepts: list[dict[str, Any]],
    *,
    available: bool,
    max_cast: int,
    safe_styles: Iterable[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return copies of ``concepts`` with ``casts_person_reference`` cleared where
    casting breaks a deterministic rule, plus one warning per change.

    A cast (``casts_person_reference is True``; any other value normalises to
    False) is cleared when no person reference is ``available``, when
    ``canonical_style(visual_style)`` is not in ``safe_styles``, when the prompt
    has no human-subject cue (``_HUMAN_CUE``), or beyond the first ``max_cast``
    valid casts in concept order. A cleared concept gets a "(Not cast: …)" suffix
    on its ``person_casting_reason`` and its prompt's reference wording becomes
    "a person" (no photo is attached to it). A kept cast whose prompt doesn't
    point at the reference image gets ``PERSON_HERO_LINE`` appended (idempotent).
    """
    from .style_shortlist import canonical_style

    safe = frozenset(safe_styles)
    out: list[dict[str, Any]] = []
    warns: list[str] = []
    kept = 0
    for concept in concepts:
        c = copy.deepcopy(concept)
        flag = c.get("casts_person_reference")
        if flag is not True:
            if flag not in (None, False):
                c["casts_person_reference"] = False
            out.append(c)
            continue
        name = c.get("concept_name", "?")
        prompt = str(c.get("image_generation_prompt") or "")
        why = ""
        if not available:
            why = "no person reference for this run"
        elif canonical_style(c.get("visual_style")) not in safe:
            why = "its style is not one of the person-safe styles"
        elif not _HUMAN_CUE.search(_PERSON_HERO_RE.sub(" ", prompt)):
            # The reference wording itself ("the person in …") doesn't count.
            why = "its prompt has no human subject"
        elif kept >= max_cast:
            why = f"at most {max(max_cast, 0)} concepts may cast the person"
        if why:
            c["casts_person_reference"] = False
            reason = str(c.get("person_casting_reason") or "").strip()
            c["person_casting_reason"] = f"{reason} (Not cast: {why}.)".strip()
            c["image_generation_prompt"] = neutralise_person_prompt(prompt)
            warns.append(f"{name}: person cast cleared ({why})")
        else:
            kept += 1
            if not _PERSON_HERO_RE.search(prompt):
                c["image_generation_prompt"] = prompt.rstrip() + PERSON_HERO_LINE
                warns.append(f"{name}: person reference wording appended")
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


def in_image_quotes(prompt: str) -> list[str]:
    """The quoted spans of ``prompt`` that are in-image text (see module doc).

    A span counts only when a ``_TEXT_CUE`` word is among the
    ``_CUE_WINDOW_WORDS`` words between the previous quote (or the start) and
    it — not an idiom (``_NOT_CUE_BEFORE``, "vibe/mood … reads") — or when it
    is joined to a preceding in-image quote (``_joins``: ``reads "A" and "B"``,
    ``reads "Go" in bold, then "Now"``).
    """
    found: list[str] = []
    prev_end = 0
    prev_in_image = False
    for match in _QUOTED.finditer(prompt):
        segment = prompt[prev_end : match.start()]
        in_image = _has_text_cue(segment) or (
            prev_in_image and prev_end > 0 and _joins(segment)
        )
        quote = (match.group(1) or match.group(2)).strip()
        if in_image and re.search(r"[A-Za-z]", quote):
            found.append(quote)
        prev_end, prev_in_image = match.end(), in_image
    return found


def _joins(segment: str) -> bool:
    """``segment`` (between two quotes) joins them into one text instruction."""
    if _QUOTE_JOINER.match(segment):
        return True
    words = _WORD.findall(segment)
    return (
        len(words) <= _JOIN_MAX_WORDS
        and all(len(w) <= _JOIN_MAX_WORD_LEN for w in words)
        and _QUOTE_JOINER_TAIL.search(segment) is not None
    )


def _has_text_cue(segment: str) -> bool:
    """A real text cue is among the last ``_CUE_WINDOW_WORDS`` words of ``segment``."""
    words = [w.lower() for w in _WORD.findall(segment)]
    for i in range(max(0, len(words) - _CUE_WINDOW_WORDS), len(words)):
        word = words[i]
        if not _TEXT_CUE.match(word):
            continue
        if i + 1 < len(words) and words[i + 1] in _NOT_CUE_BEFORE:
            continue
        if word in _SPEECH_CUES and i > 0 and words[i - 1] in _MOOD_WORDS:
            continue
        if word in _ADJACENT_ONLY_CUES and i != len(words) - 1:
            continue
        return True
    return False


def is_meme_or_comic(prompt: str, visual_style: str = "") -> bool:
    """The concept is a meme caption / comic speech bubble (exempt).

    True when ``visual_style`` contains "meme" or a meme/comic palette family
    name (case-insensitive), else when the prompt uses a narrow meme/comic
    phrase (``_MEME_OR_COMIC``).
    """
    style = visual_style.lower()
    if "meme" in style or any(
        family.lower() in style for family in EXEMPT_STYLE_FAMILIES
    ):
        return True
    if _MEME_OR_COMIC.search(prompt):
        return True
    return bool(_TOP_BOTTOM_TEXT.search(prompt) and _MEME_WORD.search(prompt))


def is_centred_hero(prompt: str) -> bool:
    """A sentence of ``prompt`` (outside quotes) describes a centred hero.

    A strong phrase ("dead centre", "centre-framed", "symmetrical hero") is
    enough; a bare "centred" needs a subject/composition word in the sentence
    and never counts as "centred between …", "camera centred on" a wide/crowd
    subject or "centred in the lower/upper/… third".
    """
    unquoted = _QUOTED.sub(" ", prompt)
    return any(
        (
            _STRONG_CENTRED.search(sentence)
            or (
                _BARE_CENTRED.search(_NOT_HERO_CENTRING.sub(" ", sentence))
                and _HERO_CONTEXT.search(sentence)
            )
        )
        and not _TEXT_WORDS.search(sentence)
        and not _NEGATED_CENTRE.search(sentence)
        and not _SMALL_IN_WIDE.search(sentence)
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


def _names_brand(quote: str, names: Iterable[str]) -> bool:
    """``quote`` and a brand/product/brand-cue name contain one another as whole
    words (normalised), so a short brand never whitelists unrelated words."""
    q = f" {_normalise(quote)} "
    if not q.strip():
        return False
    for name in names:
        n = f" {_normalise(name)} "
        if n.strip() and (q in n or n in q):
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
    brand: str = "",
    target_product: str = "",
    max_text_concepts: int = DEFAULT_MAX_TEXT_CONCEPTS,
    strictness: Sequence[str] = (),
) -> dict[str, list[CopyIssue]]:
    """The concepts' deterministic issues, keyed by ``concept_keys``.

    ``concepts`` is the ``final_visual_concepts`` state value and ``ad_copies``
    the ``ad_copy_critique`` value (see ``parse_concepts`` / ``parse_copies``).
    Per concept (meme/comic concepts exempt from the first): every in-image
    quote in ``image_generation_prompt`` (``in_image_quotes``) that does not
    name ``brand``, ``target_product`` or the concept's ``brand_cue`` matches
    the paired copy's (``original_id`` == ``ad_copy_id``) headline or call to
    action — case, punctuation and whitespace insensitive, substring either
    way; an unpaired concept is checked against its own headline/CTA fields,
    and skipped when it has neither; ``trend_motif`` is non-empty. Per set: the
    concepts with copy-matching (or, when unpaired, unchecked) in-image text
    (meme/comic exempt; brand/product quotes and concepts already flagged for
    a mismatch never count) beyond the first ``max_text_concepts``; a mismatch
    is worded "remove" (not "quote the copy") once the legitimate text
    concepts fill the cap,
    and every centred hero after the first, are flagged. ``strictness`` (the
    run's ``rating_strictness`` flags) only tightens: ``text_problem`` lowers
    the cap to ``STRICT_MAX_TEXT_CONCEPTS`` (never raising a tighter one).
    Only concepts with issues are returned ({} = clean). Never raises.
    """
    if "text_problem" in strictness:
        max_text_concepts = min(max_text_concepts, STRICT_MAX_TEXT_CONCEPTS)
    parsed = parse_concepts(concepts)
    copies = parse_copies(ad_copies)
    copies_by_id: dict[str, Mapping[str, Any]] = {}
    for ad in copies:
        copies_by_id.setdefault(str(ad.get("original_id")), ad)

    keys = concept_keys(parsed)
    # Pass 1: each concept's checked quotes and the ones that miss its copy.
    checked: list[tuple[list[str], list[str], list[str]]] = []
    for concept in parsed:
        prompt = _text(concept.get("image_generation_prompt"))
        exempt = is_meme_or_comic(prompt, _text(concept.get("visual_style")))
        names = (brand, target_product, _text(concept.get("brand_cue")))
        quotes = (
            []
            if exempt
            else [q for q in in_image_quotes(prompt) if not _names_brand(q, names)]
        )
        allowed = _paired_copy_texts(concept, copies_by_id)
        bad = [q for q in quotes if allowed and not _matches(q, allowed)]
        checked.append((quotes, allowed, bad))
    # Concepts whose text legitimately counts toward the cap; when they already
    # fill it, quoting the copy would only trade a mismatch for a cap overflow.
    legit_text = sum(1 for quotes, _, bad in checked if quotes and not bad)
    budget_spent = legit_text >= max_text_concepts

    issues: dict[str, list[CopyIssue]] = {}
    text_concepts = 0
    centred = 0
    for key, concept, (quotes, allowed, bad) in zip(keys, parsed, checked, strict=True):
        found: list[str] = []
        prompt = _text(concept.get("image_generation_prompt"))
        mismatched = bool(bad)
        for quote in bad:
            prefix = (
                f'in-image text "{quote}" is not the paired ad copy\'s '
                "headline or call to action: "
            )
            if budget_spent:
                found.append(
                    prefix + "remove the quoted text (the set already has "
                    f"{min(legit_text, max_text_concepts)} concepts with in-image "
                    "text) and leave clean negative space instead."
                )
            else:
                choices = " or ".join(f'"{t}"' for t in allowed)
                found.append(
                    prefix + f"quote {choices} exactly, or remove the quoted text."
                )

        if not _text(concept.get("trend_motif")):
            found.append(
                "trend_motif is empty: add a concrete, trend-specific visual "
                "element and write it verbatim into image_generation_prompt."
            )

        # A mismatched concept is handled by its mismatch issue (the fix keeps
        # or drops its text), so it never pushes a legitimate one over the cap.
        if quotes and not mismatched:
            text_concepts += 1
            if text_concepts > max_text_concepts:
                found.append(
                    f"in-image text appears in more than {max_text_concepts} "
                    f"concept{'' if max_text_concepts == 1 else 's'}: remove the quoted text from this concept and leave "
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
