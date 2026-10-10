"""Pure helpers for casting the user's consented person reference.

The person reference (``state["person_reference"] = {uri, consent_id}``) is seeded
via createSession initialState and consent-checked by the api at kick-off
(``runserver.async_runs``). The engine side only re-checks the shape: a ``gs://``
URI under a ``person-refs/`` folder. Person photo URIs are never logged.
"""

from __future__ import annotations

import re
from typing import Any

PERSON_REFS_SEGMENT = "person-refs/"
_PERSON_URI_RE = re.compile(r"^gs://[^/]+/person-refs/[^/]+/[^/]+$")


def person_reference_uri(value: Any) -> str:
    """The person photo's ``gs://`` URI when ``value`` is a ``{uri, ...}`` mapping
    whose URI sits under ``gs://<bucket>/person-refs/<slug>/``; ``""`` otherwise."""
    if not isinstance(value, dict):
        return ""
    uri = value.get("uri")
    if not isinstance(uri, str) or ".." in uri:
        return ""
    uri = uri.strip()
    return uri if _PERSON_URI_RE.match(uri) else ""


def person_reference_available(value: Any) -> str:
    """``"yes"`` when ``value`` names a usable person reference, else ``""`` (the
    visual agents read it as ``{person_reference_available?}``; never the URI)."""
    return "yes" if person_reference_uri(value) else ""


def person_consent_id(value: Any) -> str:
    """The person reference's ``consent_id`` (``""`` when absent)."""
    if not isinstance(value, dict):
        return ""
    consent_id = value.get("consent_id")
    return consent_id.strip() if isinstance(consent_id, str) else ""


# The role text for the attached person photo ("Reference image N (person): …"),
# following the research's references → relationship → scenario formula (§4.3).
PERSON_ROLE_INSTRUCTION = (
    "Cast this exact person as the hero: keep their face shape, skin tone, hair, "
    "eye colour, apparent age and distinguishing features; change only pose, "
    "clothing, expression and lighting as described. Do not beautify, slim or "
    "alter their body."
)

# The calibration spike confirmed gemini-nano-banana-2.1 accepts this on person
# renders (docs/notes/person-reference-calibration.md).
PERSON_GENERATION = "ALLOW_ADULT"

# Finish reasons that mean a safety filter withheld the image.
SAFETY_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "PROHIBITED_CONTENT",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "BLOCKLIST",
        "SPII",
    }
)
NO_IMAGE = "no_image"


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    text = str(value or "")
    return text.rsplit(".", 1)[-1]


def _has_image(response: Any) -> bool:
    candidates = getattr(response, "candidates", None) or []
    content = getattr(candidates[0], "content", None) if candidates else None
    for part in getattr(content, "parts", None) or []:
        inline = getattr(part, "inline_data", None)
        if inline is not None and getattr(inline, "data", None):
            return True
    return False


def person_block_reason(response: Any) -> str | None:
    """Why a person render produced no usable image, or None when it did.

    ``prompt_blocked:<reason>`` for ``prompt_feedback.block_reason``,
    ``<finish reason>`` (lower-case) for a safety finish reason on the first
    candidate, ``no_image`` when no image part came back. Never raises."""
    feedback = getattr(response, "prompt_feedback", None)
    block = getattr(feedback, "block_reason", None) if feedback else None
    if block and _enum_name(block) != "BLOCKED_REASON_UNSPECIFIED":
        return f"prompt_blocked:{_enum_name(block).lower()}"
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        finish = _enum_name(getattr(candidates[0], "finish_reason", None))
        if finish in SAFETY_FINISH_REASONS:
            return finish.lower()
    if not _has_image(response):
        return NO_IMAGE
    return None


def person_image_config(base: Any) -> Any:
    """``base`` (a genai ``ImageConfig``) with ``person_generation=ALLOW_ADULT``."""
    return base.model_copy(update={"person_generation": PERSON_GENERATION})
