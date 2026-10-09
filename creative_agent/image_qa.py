"""Post-render image QA: one vision check per rendered image.

``generate_image`` (``image_tools``) renders each concept, then
``inspect_image`` asks a vision model (``config.image_qa_model``, default the
worker flash model) whether the pixels deliver what the concept promised: the
product and trend motif recognisable, requested in-image text exact and
legible, no prominent gibberish text, no unrequested third-party logos (a live
probe saw the image model add a sports-brand swoosh), no severe artifacts,
nothing unsafe. The pure rule ``qa_failures`` turns the verdict into the
displayed issue list; ``correction_text`` builds the (quote-free) re-render
instruction. A missing brand cue is advisory only (``brand_cue_visible`` never
fails an image).

Conservative by design: QA is on by default and every false failure costs a
re-render against a ~2 RPM image quota, so the instruction gives the model the
concept's own image prompt, accepts a recognisable partial view of the
product, ignores tiny/incidental background lettering and stylised renderings
of the campaign brand's own logo, and only flags clearly recognisable marks of
brands nobody asked for (calibrated on live renders, 2026-10-07).

Fail-open by design: the caller treats any exception here as "QA unavailable"
and keeps the rendered image.
"""

import functools
import re
from collections.abc import Sequence
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from agent_common import genai_retry
from agent_common.locations import MODEL_LOCATION

from .concept_guard import in_image_quotes, is_meme_or_comic
from .config import config

__all__ = [
    "CRITICAL_RULES",
    "ISSUE_CHAR_CAP",
    "PROMPT_CHAR_CAP",
    "ImageQAResult",
    "correction_text",
    "expected_text",
    "inspect_image",
    "qa_failed_rules",
    "qa_failures",
]

# The concept's image prompt shown to the QA model is capped (chars).
PROMPT_CHAR_CAP = 1500
# Each model issue fed back into a re-render prompt is capped (chars).
ISSUE_CHAR_CAP = 120
NO_NEW_TEXT = "Do not add any new text to the image."


class ImageQAResult(BaseModel):
    """The vision model's verdict on one rendered image."""

    product_visible: bool = Field(
        description=(
            "The campaign's product is recognisable: a partial or close-up view "
            "that clearly shows its identifying features counts. For an "
            "intangible product, true if the depiction the prompt describes is "
            "present."
        )
    )
    motif_visible: bool = Field(
        description=(
            "The trend motif is visible (for an abstract motif, true if the "
            "depiction the prompt describes is present)."
        )
    )
    brand_cue_visible: bool | None = Field(
        default=None,
        description="The brand cue is visible; null when no brand cue was given.",
    )
    text_expected: bool = Field(
        description="The concept asked for specific in-image text."
    )
    text_exact: bool | None = Field(
        default=None,
        description=(
            "The requested in-image text appears exactly as written (no extra, "
            "missing or misspelled words); null when no text was requested."
        ),
    )
    text_legible: bool | None = Field(
        default=None,
        description="The requested in-image text is legible; null when none was requested.",
    )
    gibberish_text: bool = Field(
        description=(
            "Prominent, readable-size lettering is garbled, misspelled or "
            "nonsense. Tiny or blurred incidental background text and stylised "
            "renderings of the campaign brand's own logo or signature do not count."
        )
    )
    unrequested_logos: bool = Field(
        description=(
            "A clearly recognisable logo or trademark of a brand that is not "
            "allowed (see the instruction's allowed-brands list) appears."
        )
    )
    product_malformed: bool = Field(
        default=False,
        description=(
            "The product's shape is wrong: mirrored or reversed, parts missing, "
            "duplicated or in the wrong place, warped or melted."
        ),
    )
    artifacts: bool = Field(
        description="Noticeable anatomy or object deformities (extra fingers, melted or broken objects)."
    )
    unsafe: bool = Field(
        description="Sexual, violent, hateful or otherwise brand-unsafe content."
    )
    person_cast: bool | None = Field(
        default=None,
        description=(
            "The image's hero is a person shown as the person reference image "
            "intends; null when no person reference image was supplied."
        ),
    )
    person_likeness: bool | None = Field(
        default=None,
        description=(
            "The hero is clearly the same person as the person reference image; "
            "null when no person reference image was supplied."
        ),
    )
    person_distorted: bool = Field(
        default=False,
        description=(
            "The cast person's face or body is distorted, malformed or altered "
            "(slimmed, beautified, added attributes)."
        ),
    )
    issues: list[str] = Field(
        default_factory=list,
        description=(
            "Short problem statements in the form '[what is wrong] on/in [where]', "
            "one per problem found; empty when the image passes."
        ),
    )


def expected_text(concept: dict[str, Any]) -> list[str] | None:
    """The in-image text the concept's prompt asks for, or None — pure.

    Reuses ``concept_guard.in_image_quotes`` (a quoted span after a text cue
    such as "reads"/"headline"); descriptive quotes are ignored.
    """
    quotes = in_image_quotes(concept.get("image_generation_prompt") or "")
    return quotes or None


_ANY_QUOTE = re.compile(r'"[^"\n]+"|“[^”\n]+”')


def _is_meme_or_comic(concept: dict[str, Any]) -> bool:
    return is_meme_or_comic(
        concept.get("image_generation_prompt") or "",
        concept.get("visual_style") or "",
    )


def _text_required(result: ImageQAResult, concept: dict[str, Any]) -> bool:
    """Whether the exact/legible text checks apply — pure.

    ``expected_text`` is authoritative; the model's ``text_expected`` only
    counts for a meme/comic concept whose prompt quotes text (a caption or
    speech bubble ``in_image_quotes`` may not catch).
    """
    if expected_text(concept) is not None:
        return True
    prompt = concept.get("image_generation_prompt") or ""
    return bool(
        result.text_expected
        and _is_meme_or_comic(concept)
        and _ANY_QUOTE.search(prompt)
    )


# (attribute, rule name, correction phrase) per check, in report order.
_RULES: tuple[tuple[str, str, str], ...] = (
    ("product_visible", "product not visible", "show the product recognisably"),
    ("motif_visible", "trend motif not visible", "show the trend motif clearly"),
    (
        "product_malformed",
        "malformed product",
        "render the product with its correct real-world shape and orientation",
    ),
    (
        "gibberish_text",
        "gibberish text",
        "remove garbled or misspelled lettering",
    ),
    (
        "unrequested_logos",
        "unrequested third-party logo",
        "remove third-party logos",
    ),
    ("artifacts", "severe visual artifacts", "fix severe visual artifacts"),
    ("unsafe", "unsafe content", "remove brand-unsafe content"),
)
_TEXT_RULES: dict[str, str] = {
    "gibberish text": "remove garbled or misspelled lettering",
    "in-image text not exact": (
        "render only the in-image text the prompt quotes, spelled exactly"
    ),
    "in-image text not legible": "make the requested in-image text legible",
}
# Person casting (only for a concept with casts_person_reference): neither is
# critical. The likeness phrase restates the render's person instruction.
PERSON_LIKENESS_PHRASE = (
    "keep the hero exactly the same person as the person reference image: their "
    "face shape, skin tone, hair, eye colour, apparent age and distinguishing "
    "features"
)
_PERSON_RULES: dict[str, str] = {
    "person likeness lost": PERSON_LIKENESS_PHRASE,
    "person distorted": (
        "render the person's face and body naturally and undistorted, without "
        "slimming, beautifying or adding attributes"
    ),
}
# Failures weighed first when choosing between attempts (image_tools).
CRITICAL_RULES = frozenset({"unsafe content", "unrequested third-party logo"})


def is_cast(concept: dict[str, Any]) -> bool:
    """Whether the concept casts the user's person reference."""
    return concept.get("casts_person_reference") is True


def qa_failed_rules(
    result: ImageQAResult, concept: dict[str, Any], *, target_product: str
) -> list[str]:
    """The names of the failed checks — pure; ``[]`` means the image passes.

    Fails on: product or motif not visible (skipped when ``target_product`` /
    the concept's ``trend_motif`` is empty — nothing was promised), a
    malformed product (also skipped without ``target_product``), gibberish
    text, an unrequested logo, severe artifacts, unsafe content, or — when
    text is required (``_text_required``) — text not exact or not legible
    (``None`` = unknown, not a failure). ``brand_cue_visible`` is advisory and
    never fails.
    """
    skip = set()
    if not (concept.get("trend_motif") or "").strip():
        skip.add("motif_visible")
    if not (target_product or "").strip():
        skip.update({"product_visible", "product_malformed"})
    failed = []
    for attr, name, _ in _RULES:
        if attr in skip:
            continue
        value = getattr(result, attr)
        # *_visible must be True; the rest must be False.
        if (attr.endswith("_visible") and not value) or (
            not attr.endswith("_visible") and value
        ):
            failed.append(name)
    if _text_required(result, concept):
        if result.text_exact is False:
            failed.append("in-image text not exact")
        if result.text_legible is False:
            failed.append("in-image text not legible")
    if is_cast(concept):
        if result.person_likeness is False:
            failed.append("person likeness lost")
        if result.person_distorted:
            failed.append("person distorted")
    return failed


def qa_failures(
    result: ImageQAResult, concept: dict[str, Any], *, target_product: str
) -> list[str]:
    """Displayed issue strings for a failed image (``[]`` when it passes) — pure.

    Prefers the model's own short problem statements; falls back to the rule
    names when it gave none. Model issues alone (with no failed rule, e.g. a
    missing brand cue) never fail an image. Not fed back verbatim into the
    re-render prompt — see ``correction_text``.
    """
    failed = qa_failed_rules(result, concept, target_product=target_product)
    if not failed:
        return []
    issues = [i.strip() for i in result.issues if i and i.strip()]
    return issues or failed


_QUOTE_CHARS = re.compile("[\"'`“”‘’«»]")


def _clean_issue(issue: str) -> str:
    """A model issue made safe for a render prompt: quotes stripped, capped."""
    cleaned = " ".join(_QUOTE_CHARS.sub("", issue).split())
    return cleaned[:ISSUE_CHAR_CAP].rstrip()


def correction_text(
    result: ImageQAResult, concept: dict[str, Any], *, target_product: str
) -> str:
    """The instruction appended to the prompt for a re-render — pure.

    Never injects strings into the render: when a text rule failed, the
    corrections are fixed rule phrases (the model's issue may quote the
    garbled lettering); otherwise the model's problem statements with quote
    characters stripped, each capped at ``ISSUE_CHAR_CAP`` (rule phrases when
    it gave none). Always ends with "Do not add any new text to the image."
    """
    failed = qa_failed_rules(result, concept, target_product=target_product)
    phrases = {name: phrase for _, name, phrase in _RULES} | _TEXT_RULES | _PERSON_RULES
    if any(name in _TEXT_RULES for name in failed):
        items = [phrases[name] for name in failed]
    else:
        items = [c for c in (_clean_issue(i) for i in result.issues if i) if c]
        items = items or [phrases[name] for name in failed]
    # A person rule always restates its fixed instruction (the likeness ask).
    items += [phrases[name] for name in failed if name in _PERSON_RULES]
    items = list(dict.fromkeys(items))
    return (
        "Correct these issues from the previous attempt: "
        + "; ".join(items)
        + ". "
        + NO_NEW_TEXT
    )


# Rating strictness (opt-in rating learning): recurring "product hard to see"
# / "trend unclear" fail reasons raise the bar for the matching verdict.
PROMINENT_PRODUCT_RULE = (
    "The product must be prominent: this brand's reviewers often found it hard "
    "to see, so product_visible is true only when the product is large and "
    "clearly in the foreground, not small, distant or mostly hidden."
)
PROMINENT_MOTIF_RULE = (
    "The trend motif must be prominent: this brand's reviewers often found the "
    "trend unclear, so motif_visible is true only when the motif is clearly "
    "visible and easy to spot, not small or incidental in the background."
)


# Cast concepts: the person photo is attached after the rendered image (a
# vision-LLM comparison, deliberately no face embeddings / biometrics).
PERSON_COMPARISON_LINE = (
    "The second attached image is the person reference: the render's hero was "
    "asked to be this exact person. person_cast: true when the image's hero is "
    "a single clearly visible person. person_likeness: Is the hero clearly the "
    "same person as the reference image? true or false (judge face shape, skin "
    "tone, hair, eye colour, apparent age and distinguishing features; ignore "
    "pose, clothing, expression and lighting). person_distorted: true when the "
    "person's face or body is distorted or malformed, or visibly altered "
    "(slimmed, beautified, added attributes)."
)
NO_PERSON_LINE = (
    "No person reference image: set person_cast and person_likeness null and "
    "person_distorted false."
)


def _instruction(
    concept: dict[str, Any],
    *,
    brand: str,
    target_product: str,
    has_logo_reference: bool = False,
    strictness: Sequence[str] = (),
    has_person_image: bool = False,
) -> str:
    """The QA instruction for one concept (what to check, what was promised).

    ``strictness`` (the run's ``rating_strictness`` flags; default none) only
    raises the bar: ``product_not_visible`` / ``trend_unclear`` ask for a
    *prominent* product / motif. ``qa_failed_rules`` is unchanged — the
    stricter verdict simply arrives as ``product_visible`` / ``motif_visible``.
    """
    prompt = " ".join((concept.get("image_generation_prompt") or "").split())
    motif = (concept.get("trend_motif") or "").strip()
    lines = [
        "You are an ad-image QA reviewer. Inspect the attached rendered ad "
        "image and fill in the JSON verdict. Flag only clear, material "
        "problems: every failure triggers a costly re-render.",
        f"Campaign brand: {brand or 'unknown'}.",
        f"What the image was asked to show: {prompt[:PROMPT_CHAR_CAP] or 'not given'}",
    ]
    if target_product:
        lines.append(
            f"Product: {target_product}. product_visible is true when the "
            "product is recognisable — a partial or close-up view that clearly "
            "shows its identifying features (e.g. a distinctive headstock, "
            "logo or shape) counts; it need not be shown in full. For an "
            "intangible product (an app, service or subscription), true if the "
            "depiction described above is present."
        )
        if "product_not_visible" in strictness:
            lines.append(PROMINENT_PRODUCT_RULE)
    else:
        lines.append("No specific product: set product_visible true.")
    if motif:
        lines.append(
            f"Trend motif: {motif}. motif_visible is true when it is visible; "
            "for an abstract motif, true if the depiction described above is "
            "present."
        )
        if "trend_unclear" in strictness:
            lines.append(PROMINENT_MOTIF_RULE)
    else:
        lines.append("No trend motif: set motif_visible true.")
    brand_cue = (concept.get("brand_cue") or "").strip()
    if brand_cue:
        lines.append(f"Brand cue that should be visible: {brand_cue}.")
    else:
        lines.append("brand_cue_visible: null (nothing to check).")
    text = expected_text(concept)
    if text:
        quoted = "; ".join(f'"{t}"' for t in text)
        lines.append(
            f"Requested in-image text (must appear exactly and legibly): {quoted}."
        )
    else:
        lines.append(
            "No in-image text was requested: set text_expected false and "
            "text_exact/text_legible null."
        )
    lines.append(
        "gibberish_text: true only for prominent, readable-size lettering that "
        "is garbled, misspelled or nonsense. Ignore tiny or blurred incidental "
        "text in the background (e.g. labels on equipment, distant signs) and "
        "stylised renderings of the campaign brand's own logo or signature."
    )
    if _is_meme_or_comic(concept):
        lines.append(
            "This is a meme/comic concept: deliberate slang or meme spelling in "
            "captions or speech bubbles is not gibberish."
        )
    allowed = [f"the campaign brand ({brand or 'unknown'})"]
    if target_product:
        allowed.append(f"the maker of the product ({target_product})")
    allowed.append("any brand named in the prompt above or in its quoted text")
    if brand_cue:
        allowed.append(f"the brand cue ({brand_cue})")
    if has_logo_reference:
        allowed.append("the logo reference image supplied with the prompt")
    lines += [
        "unrequested_logos: true only for a clearly recognisable logo, "
        "wordmark or trademark of another company (e.g. a sports-brand "
        "swoosh or a third-party badge on equipment). Allowed: "
        + "; ".join(allowed)
        + ". A maker's badge, nameplate or script logo on third-party "
        "equipment (e.g. on an amplifier) counts even if partly illegible; "
        "plain unbranded labels and tiny incidental text do not.",
        "product_malformed: true when the product's own shape is wrong — "
        "mirrored or reversed (e.g. a guitar whose headstock, neck or controls "
        "are flipped, or reversed lettering on it), parts missing, duplicated or "
        "in the wrong place, warped or melted. Deliberate stylisation (cartoon, "
        "comic, collage) is not a defect.",
        "Also flag noticeable anatomy or object deformities (artifacts) and "
        "brand-unsafe content.",
        PERSON_COMPARISON_LINE if has_person_image else NO_PERSON_LINE,
        "issues: short problem statements in the form '[what is wrong] on/in "
        "[where]', one per problem found; empty if none.",
    ]
    return "\n".join(lines)


@functools.cache
def _get_qa_client() -> genai.Client:
    """The genai client for the QA model (cached; global; retry + timeout).

    Same shape as the creative_eval judge client: status-code HTTP retry for
    429/5xx and the per-request timeout (MILLISECONDS) — model-aware, so the
    default flash QA model gets the shorter flash timeout.
    """
    return genai.Client(
        vertexai=True,
        project=config.PROJECT_ID,
        location=MODEL_LOCATION,
        http_options=types.HttpOptions(
            retry_options=genai_retry.build_genai_http_retry(),
            timeout=genai_retry.model_request_timeout_ms(config.image_qa_model),
        ),
    )


def inspect_image(
    image_bytes: bytes,
    mime: str,
    concept: dict[str, Any],
    *,
    brand: str,
    target_product: str,
    client: Any,
    model: str,
    has_logo_reference: bool = False,
    strictness: Sequence[str] = (),
    person_image: tuple[bytes, str] | None = None,
) -> ImageQAResult:
    """One structured vision call → the image's ``ImageQAResult``.

    ``person_image`` (``(bytes, mime)`` of the consented person photo; only for
    a cast concept) is attached after the render with the likeness question.
    Synchronous (the genai sync client); call it via ``asyncio.to_thread``.
    Raises on any API/parse error — the caller fails open.
    """
    parts = [types.Part.from_bytes(data=image_bytes, mime_type=mime)]
    if person_image is not None:
        parts.append(
            types.Part.from_bytes(data=person_image[0], mime_type=person_image[1])
        )
    parts.append(
        types.Part.from_text(
            text=_instruction(
                concept,
                brand=brand,
                target_product=target_product,
                has_logo_reference=has_logo_reference,
                strictness=strictness,
                has_person_image=person_image is not None,
            )
        )
    )
    response = client.models.generate_content(
        model=model,
        contents=parts,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ImageQAResult,
            temperature=0,
        ),
    )
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, ImageQAResult):
        return parsed
    return ImageQAResult.model_validate_json(response.text or "")
