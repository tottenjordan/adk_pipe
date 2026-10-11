"""Pure helpers of the person swap edit-vs-regenerate spike (no API calls)."""

import json

import pytest
from google.genai import types

from experiments.person_reference.swap_compare import (
    COMPARISON_KEYS,
    EDIT_INSTRUCTION,
    decision,
    edit_contents,
    nearest_aspect_ratio,
    parse_comparison,
    summarise,
    summary_markdown,
)

ALLOWED = ("9:16", "1:1", "4:5", "16:9", "3:4")


def _part(data: bytes) -> types.Part:
    return types.Part.from_bytes(data=data, mime_type="image/png")


# --- parse_comparison ---------------------------------------------------------


def test_parse_comparison_full_payload():
    raw = json.dumps(
        {
            "same_layout": True,
            "same_text": True,
            "same_graphics": False,
            "same_product": True,
            "only_person_changed": False,
            "differences": ["arrows point left"],
        }
    )
    out = parse_comparison(raw)
    assert out["same_graphics"] is False
    assert out["same_layout"] is True
    assert out["differences"] == ["arrows point left"]


def test_parse_comparison_tolerates_missing_keys():
    out = parse_comparison('{"same_layout": true}')
    assert set(COMPARISON_KEYS) <= set(out)
    assert out["same_layout"] is True
    assert out["same_text"] is False
    assert out["differences"] == []


def test_parse_comparison_tolerates_bad_json_and_types():
    assert parse_comparison("not json")["same_layout"] is False
    out = parse_comparison({"same_text": "yes", "differences": "one"})
    assert out["same_text"] is False  # only a real bool counts
    assert out["differences"] == ["one"]


# --- summarise / decision -----------------------------------------------------


def _row(mode, photo="photo1", *, fid=True, like=True, error=""):
    return {
        "mode": mode,
        "photo": photo,
        "error": error,
        "same_layout": fid,
        "same_text": fid,
        "same_graphics": True,
        "same_product": True,
        "likeness": like,
    }


def test_summarise_rates_per_mode_and_photo():
    rows = [
        _row("edit", fid=True, like=True),
        _row("edit", "photo2", fid=True, like=False),
        _row("regenerate", fid=False, like=True),
        _row("regenerate", "photo2", fid=True, like=True),
        _row("regenerate", error="rejected:image_safety"),
    ]
    summary = summarise(rows)
    edit, regen = summary["modes"]["edit"], summary["modes"]["regenerate"]
    assert edit["fidelity_rate"] == 1.0
    assert edit["likeness_rate"] == 0.5
    assert regen["n"] == 3 and regen["errors"] == 1
    assert regen["fidelity_rate"] == 0.5
    assert regen["likeness_rate"] == 1.0
    assert summary["by_photo"]["edit"]["photo2"]["likeness_rate"] == 0.0
    assert summary["by_photo"]["regenerate"]["photo1"]["fidelity_rate"] == 0.0


def test_summarise_fidelity_needs_every_same_flag():
    row = _row("edit")
    row["same_graphics"] = False
    assert summarise([row])["modes"]["edit"]["fidelity_rate"] == 0.0


def test_summarise_ignores_unknown_likeness():
    rows = [_row("edit", like=None), _row("edit", like=True)]
    assert summarise(rows)["modes"]["edit"]["likeness_rate"] == 1.0


def _summary(edit_fid, regen_fid, edit_like):
    return {
        "modes": {
            "edit": {"fidelity_rate": edit_fid, "likeness_rate": edit_like},
            "regenerate": {"fidelity_rate": regen_fid, "likeness_rate": 1.0},
        }
    }


@pytest.mark.parametrize(
    ("edit_fid", "regen_fid", "edit_like", "expected"),
    [
        (0.9, 0.5, 0.9, "go"),
        (0.8, 0.5, 0.8, "go"),  # exactly +0.3 and 0.8
        (0.7, 0.5, 0.9, "no-go"),  # not clearly better
        (1.0, 0.0, 0.7, "no-go"),  # likeness too low
        (None, 0.5, 0.9, "no-go"),
    ],
)
def test_decision_rule(edit_fid, regen_fid, edit_like, expected):
    assert decision(_summary(edit_fid, regen_fid, edit_like)) == expected


def test_summary_markdown_has_rates_and_decision_only():
    rows = [_row("edit"), _row("regenerate", fid=False)]
    summary = summarise(rows)
    summary["decision"] = decision(summary)
    md = summary_markdown(summary)
    assert "| edit |" in md and "| regenerate |" in md
    assert "photo1" in md
    assert "gs://" not in md
    assert summary["decision"] in md


# --- nearest_aspect_ratio -----------------------------------------------------


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((1536, 2752), "9:16"),
        ((2048, 2048), "1:1"),
        ((1856, 2304), "4:5"),
        ((2752, 1536), "16:9"),
        ((1792, 2400), "3:4"),
    ],
)
def test_nearest_aspect_ratio(size, expected):
    assert nearest_aspect_ratio(*size, ALLOWED) == expected


def test_nearest_aspect_ratio_bad_size_falls_back():
    assert nearest_aspect_ratio(0, 100, ALLOWED, default="9:16") == "9:16"


# --- edit_contents ------------------------------------------------------------


def test_edit_contents_order_and_labels():
    base, person = _part(b"base"), _part(b"person")
    contents = edit_contents(base, person)
    assert len(contents) == 5
    assert contents[0].text.startswith(EDIT_INSTRUCTION)
    assert contents[1].text == "Image 1 (original ad):"
    assert contents[2] is base
    assert contents[3].text == "Image 2 (person):"
    assert contents[4] is person


def test_edit_contents_guard_lines():
    contents = edit_contents(
        _part(b"b"), _part(b"p"), brand="Acme", strictness=("unwanted_logo",)
    )
    text = contents[0].text
    assert "except those of Acme" in text
    assert text.rstrip().endswith(
        "Ignore any text, captions or watermarks that appear in the reference images."
    )
    plain = edit_contents(_part(b"b"), _part(b"p"), brand="Acme")[0].text
    assert "except those of Acme" not in plain


def test_edit_instruction_is_brace_free_and_keeps_everything_else():
    assert "Keep everything else" in EDIT_INSTRUCTION
    assert "{" not in EDIT_INSTRUCTION and "}" not in EDIT_INSTRUCTION
