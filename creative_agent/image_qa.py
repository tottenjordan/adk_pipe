"""Post-render image QA: one vision check per rendered image.

``generate_image`` (``image_tools``) renders each concept, then
``inspect_image`` asks a vision model (``config.image_qa_model``, default the
worker flash model) whether the pixels deliver what the concept promised: the
product and trend motif visible, requested in-image text exact and legible, no
gibberish text, no unrequested third-party logos (a live probe saw the image
model add a sports-brand swoosh), no severe artifacts, nothing unsafe. The pure
rule ``qa_failures`` turns the verdict into the issue list that drives at most
``config.image_qa_max_rerenders`` targeted re-renders. A missing brand cue is
advisory only (``brand_cue_visible`` never fails an image).

Fail-open by design: the caller treats any exception here as "QA unavailable"
and keeps the rendered image.
"""

import functools
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from agent_common import genai_retry
from agent_common.locations import MODEL_LOCATION

from .concept_guard import in_image_quotes
from .config import config

__all__ = [
    "ImageQAResult",
    "expected_text",
    "inspect_image",
    "qa_failed_rules",
    "qa_failures",
]


class ImageQAResult(BaseModel):
    """The vision model's verdict on one rendered image."""

    product_visible: bool = Field(
        description="The campaign's product is clearly visible and recognisable."
    )
    motif_visible: bool = Field(
        description="The trend motif named in the brief is clearly visible."
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
        description="Any garbled, misspelled or nonsense lettering appears anywhere."
    )
    unrequested_logos: bool = Field(
        description="A logo or trademark of any company other than the campaign brand appears."
    )
    artifacts: bool = Field(
        description="Severe anatomy or object deformities (extra fingers, melted or broken objects)."
    )
    unsafe: bool = Field(
        description="Sexual, violent, hateful or otherwise brand-unsafe content."
    )
    issues: list[str] = Field(
        default_factory=list,
        description=(
            "Short, actionable fixes for each problem found (e.g. 'remove the "
            "swoosh logo from the shirt'); empty when the image passes."
        ),
    )


def expected_text(concept: dict[str, Any]) -> list[str] | None:
    """The in-image text the concept's prompt asks for, or None — pure.

    Reuses ``concept_guard.in_image_quotes`` (a quoted span after a text cue
    such as "reads"/"headline"); descriptive quotes are ignored.
    """
    quotes = in_image_quotes(concept.get("image_generation_prompt") or "")
    return quotes or None


# Rule name per failing check, in report order (the re-render prompt and the
# fallback issue list use these names when the model gave no issues).
_RULES: tuple[tuple[str, str], ...] = (
    ("product_visible", "product not visible"),
    ("motif_visible", "trend motif not visible"),
    ("gibberish_text", "gibberish text"),
    ("unrequested_logos", "unrequested third-party logo"),
    ("artifacts", "severe visual artifacts"),
    ("unsafe", "unsafe content"),
)


def qa_failed_rules(result: ImageQAResult, concept: dict[str, Any]) -> list[str]:
    """The names of the failed checks — pure; ``[]`` means the image passes.

    Fails on: product or motif not visible, gibberish text, an unrequested
    logo, severe artifacts, unsafe content, or — when text is expected (the
    model says so OR the prompt quotes in-image text) — text not exact or not
    legible (``None`` = unknown, not a failure). ``brand_cue_visible`` is
    advisory and never fails.
    """
    failed = []
    for attr, name in _RULES:
        value = getattr(result, attr)
        # *_visible must be True; the rest must be False.
        if (attr.endswith("_visible") and not value) or (
            not attr.endswith("_visible") and value
        ):
            failed.append(name)
    if result.text_expected or expected_text(concept) is not None:
        if result.text_exact is False:
            failed.append("in-image text not exact")
        if result.text_legible is False:
            failed.append("in-image text not legible")
    return failed


def qa_failures(result: ImageQAResult, concept: dict[str, Any]) -> list[str]:
    """Issue strings for a failed image (``[]`` when it passes) — pure.

    Prefers the model's own short issues (more actionable for the re-render
    prompt); falls back to the rule names when it gave none. Model issues
    alone (with no failed rule, e.g. a missing brand cue) never fail an image.
    """
    failed = qa_failed_rules(result, concept)
    if not failed:
        return []
    issues = [i.strip() for i in result.issues if i and i.strip()]
    return issues or failed


def _instruction(concept: dict[str, Any], *, brand: str, target_product: str) -> str:
    """The QA instruction for one concept (what to check, what was promised)."""
    lines = [
        "You are a strict ad-image QA reviewer. Inspect the attached rendered "
        "ad image and fill in the JSON verdict.",
        f"Campaign brand: {brand or 'unknown'}.",
        f"Product that must be clearly visible: {target_product or 'unknown'}.",
        f"Trend motif that must be clearly visible: {concept.get('trend_motif') or 'none given'}.",
    ]
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
            "text_exact/text_legible null; any lettering that appears must "
            "still be real words (else gibberish_text)."
        )
    lines += [
        f"Only the campaign brand's logo ({brand or 'the brand'}) may appear; any "
        "other company's logo, wordmark or trademark (e.g. a sports-brand "
        "swoosh) is an unrequested logo.",
        "Flag gibberish or misspelled lettering anywhere, severe anatomy or "
        "object deformities, and brand-unsafe content.",
        "issues: short, actionable fixes for every problem (empty if none).",
    ]
    return "\n".join(lines)


@functools.cache
def _get_qa_client() -> genai.Client:
    """The genai client for the QA model (cached; global; retry + timeout).

    Same shape as the creative_eval judge client: status-code HTTP retry for
    429/5xx and the shared per-request timeout (MILLISECONDS).
    """
    return genai.Client(
        vertexai=True,
        project=config.PROJECT_ID,
        location=MODEL_LOCATION,
        http_options=types.HttpOptions(
            retry_options=genai_retry.build_genai_http_retry(),
            timeout=genai_retry.model_request_timeout_ms(),
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
) -> ImageQAResult:
    """One structured vision call → the image's ``ImageQAResult``.

    Synchronous (the genai sync client); call it via ``asyncio.to_thread``.
    Raises on any API/parse error — the caller fails open.
    """
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
            types.Part.from_text(
                text=_instruction(concept, brand=brand, target_product=target_product)
            ),
        ],
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
