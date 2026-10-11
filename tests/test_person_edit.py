"""creative_agent/person_edit.py: the person-swap edit request (pure)."""

from __future__ import annotations

from google.genai import types

from creative_agent.image_tools import REFERENCE_IGNORE_TEXT_LINE
from creative_agent.person_edit import (
    BASE_LABEL,
    EDIT_INSTRUCTION,
    PERSON_LABEL,
    edit_contents,
    nearest_aspect_ratio,
)

BASE = types.Part.from_bytes(data=b"base", mime_type="image/png")
PHOTO = types.Part.from_bytes(data=b"face", mime_type="image/jpeg")
ALLOWED = ("1:1", "4:5", "9:16", "16:9", "3:4")


def test_instruction_keeps_everything_else_and_is_brace_free():
    assert EDIT_INSTRUCTION.startswith("Edit image 1. Replace only the main person")
    assert "Keep everything else in image 1 exactly as it is" in EDIT_INSTRUCTION
    assert "every prop and its count" in EDIT_INSTRUCTION
    assert "{" not in EDIT_INSTRUCTION and "}" not in EDIT_INSTRUCTION


def test_contents_put_the_original_before_the_person_each_labelled():
    text, base_label, base, person_label, person = edit_contents(BASE, PHOTO)
    assert text.text == EDIT_INSTRUCTION + "\n\n" + REFERENCE_IGNORE_TEXT_LINE
    assert base_label.text == BASE_LABEL == "Image 1 (original ad):"
    assert person_label.text == PERSON_LABEL == "Image 2 (person):"
    assert (base, person) == (BASE, PHOTO)


def test_logo_line_only_under_unwanted_logo_strictness():
    plain = edit_contents(BASE, PHOTO, brand="PRS")[0].text
    assert "No logos" not in plain
    strict = edit_contents(BASE, PHOTO, brand="PRS", strictness=("unwanted_logo",))
    assert strict[0].text == "\n\n".join(
        [
            EDIT_INSTRUCTION,
            "No logos, brand marks or trademarks except those of PRS.",
            REFERENCE_IGNORE_TEXT_LINE,
        ]
    )


def test_custom_instruction_carries_a_correction():
    contents = edit_contents(BASE, PHOTO, instruction=EDIT_INSTRUCTION + "\n\nFix X.")
    assert contents[0].text.startswith(EDIT_INSTRUCTION + "\n\nFix X.\n\n")


def test_nearest_aspect_ratio():
    assert nearest_aspect_ratio(1080, 1920, ALLOWED) == "9:16"
    assert nearest_aspect_ratio(1000, 1010, ALLOWED) == "1:1"
    assert nearest_aspect_ratio(1600, 2000, ALLOWED) == "4:5"
    assert nearest_aspect_ratio(0, 100, ALLOWED, default="1:1") == "1:1"
    assert nearest_aspect_ratio(100, 100, (), default="9:16") == "9:16"
