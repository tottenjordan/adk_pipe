"""Pure helpers of the person-reference calibration spike (no API calls)."""

from types import SimpleNamespace

import pytest
from google.genai import types

from creative_agent.style_shortlist import ALL_FAMILIES
from experiments.person_reference.calibrate import (
    FRAMINGS,
    STYLES,
    block_reason,
    build_grid,
    photo_uri_errors,
    summarise,
    summary_markdown,
)

PHOTOS = ["gs://b/person-refs/u/a.jpg", "gs://b/person-refs/u/b.jpg"]


def _resp(*, prompt_block=None, finish=None, image=False, thought_only=False):
    parts = []
    if image:
        parts.append(
            types.Part(
                inline_data=types.Blob(data=b"png", mime_type="image/png"),
                thought=thought_only or None,
            )
        )
    candidates = None
    if finish or parts:
        candidates = [
            types.Candidate(
                content=types.Content(role="model", parts=parts or None),
                finish_reason=finish,
            )
        ]
    feedback = (
        types.GenerateContentResponsePromptFeedback(block_reason=prompt_block)
        if prompt_block
        else None
    )
    return types.GenerateContentResponse(
        candidates=candidates, prompt_feedback=feedback
    )


# --- build_grid ---------------------------------------------------------------


def test_build_grid_covers_styles_framings_photos():
    grid = build_grid(PHOTOS)
    assert len(grid) == 12
    assert {g.framing for g in grid} == {"close", "mid"}
    assert {g.style for g in grid} == set(STYLES)
    assert {g.photo_uri for g in grid} == set(PHOTOS)
    assert len({(g.style, g.framing, g.photo_uri) for g in grid}) == 12


def test_styles_are_canonical_photographic_families():
    assert len(STYLES) == 3
    assert set(STYLES) <= set(ALL_FAMILIES)
    assert "Candid 35mm film photo" in STYLES
    assert FRAMINGS == ("close", "mid")


def test_grid_prompts_name_style_framing_and_person_reference():
    grid = build_grid(PHOTOS[:1])
    for item in grid:
        assert item.style in item.prompt
        assert "the person in the person reference image" in item.prompt
        assert "{" not in item.prompt
    prompts = {(g.style, g.framing): g.prompt for g in grid}
    assert len(set(prompts.values())) == 6
    assert "close-up" in prompts[(STYLES[0], "close")].lower()
    assert "mid shot" in prompts[(STYLES[0], "mid")].lower()


def test_build_grid_empty_photos():
    assert build_grid([]) == []


# --- block_reason -------------------------------------------------------------


def test_block_reason_reads_prompt_feedback_and_finish_reason():
    assert (
        block_reason(_resp(prompt_block="PROHIBITED_CONTENT"))
        == "prompt:PROHIBITED_CONTENT"
    )
    assert block_reason(_resp(finish="IMAGE_SAFETY")) == "finish:IMAGE_SAFETY"
    assert block_reason(_resp(image=True)) is None


@pytest.mark.parametrize(
    "finish",
    [
        "SAFETY",
        "PROHIBITED_CONTENT",
        "IMAGE_PROHIBITED_CONTENT",
        "OTHER",
        "IMAGE_OTHER",
    ],
)
def test_block_reason_block_like_finish_reasons(finish):
    assert block_reason(_resp(finish=finish)) == f"finish:{finish}"


def test_block_reason_safety_finish_wins_over_image():
    assert block_reason(_resp(finish="IMAGE_SAFETY", image=True)) == (
        "finish:IMAGE_SAFETY"
    )


def test_block_reason_image_with_stop_is_none():
    assert block_reason(_resp(finish="STOP", image=True)) is None


def test_block_reason_no_image():
    assert block_reason(_resp(finish="STOP")) == "no_image"
    assert block_reason(types.GenerateContentResponse()) == "no_image"


def test_block_reason_thought_only_image_still_counts():
    assert block_reason(_resp(image=True, thought_only=True)) is None


def test_block_reason_simple_fakes():
    fake = SimpleNamespace(
        prompt_feedback=SimpleNamespace(block_reason="SAFETY"), candidates=[]
    )
    assert block_reason(fake) == "prompt:SAFETY"
    part = SimpleNamespace(
        inline_data=SimpleNamespace(data=b"x", mime_type="image/png"), thought=False
    )
    ok = SimpleNamespace(
        prompt_feedback=None,
        candidates=[
            SimpleNamespace(finish_reason=None, content=SimpleNamespace(parts=[part]))
        ],
    )
    assert block_reason(ok) is None


# --- summarise ----------------------------------------------------------------


def test_summarise_likeness_rate_by_style():
    s = summarise(
        [
            {"style": "editorial", "likeness": True},
            {"style": "editorial", "likeness": False},
        ]
    )
    assert s["likeness_by_style"]["editorial"] == 0.5


def _row(style, framing, allow_adult, block=None, likeness=None, error=""):
    return {
        "style": style,
        "framing": framing,
        "allow_adult": allow_adult,
        "block_reason": block,
        "likeness": likeness,
        "error": error,
    }


def test_summarise_block_rates_and_framing():
    rows = [
        _row("A", "close", True, likeness=True),
        _row("A", "mid", True, block="finish:IMAGE_SAFETY"),
        _row("B", "close", False, likeness=True),
        _row("B", "mid", False, likeness=False),
    ]
    s = summarise(rows)
    assert s["n"] == 4
    assert s["likeness_by_framing"] == {"close": 1.0, "mid": 0.0}
    assert s["likeness_by_style"] == {"A": 1.0, "B": 0.5}
    assert s["block_rate"] == {
        "overall": 0.25,
        "with_allow_adult": 0.5,
        "without_allow_adult": 0.0,
    }
    assert s["false_positive_blocks"] == 1


def test_summarise_errors_excluded_from_rates():
    rows = [
        _row("A", "close", True, error="allow_adult_unsupported"),
        _row("A", "close", False, likeness=True),
    ]
    s = summarise(rows)
    assert s["n"] == 2
    assert s["errors"] == 1
    assert s["allow_adult_unsupported"] is True
    assert s["block_rate"]["with_allow_adult"] is None
    assert s["block_rate"]["overall"] == 0.0


def test_summarise_go_no_go():
    go = summarise([_row("A", "close", False, likeness=True)] * 3)
    assert go["decision"] == "go"
    assert go["person_safe_styles"] == ["A"]
    low = summarise(
        [_row("A", "close", False, likeness=False)] * 2
        + [_row("A", "close", False, likeness=True)]
    )
    assert low["decision"] == "no-go"
    blocked = summarise(
        [_row("A", "close", False, likeness=True)] * 3
        + [_row("A", "close", False, block="prompt:SAFETY")]
    )
    assert blocked["block_rate"]["overall"] == 0.25
    assert blocked["decision"] == "no-go"
    assert summarise([])["decision"] == "inconclusive"


def test_summary_markdown_has_no_photo_uris():
    rows = [_row("A", "close", False, likeness=True)]
    rows[0]["photo_uri"] = PHOTOS[0]
    md = summary_markdown(summarise(rows))
    assert "gs://" not in md
    assert "Decision" in md


# --- photo URI validation -----------------------------------------------------


def test_photo_uri_errors():
    assert photo_uri_errors(["gs://bkt/person-refs/a/x.jpg"], "bkt") == []
    errs = photo_uri_errors(
        ["gs://other/person-refs/a/x.jpg", "gs://bkt/refs/x.jpg", "/tmp/x.jpg"],
        "bkt",
    )
    assert len(errs) == 3
    assert photo_uri_errors(["gs://bkt/person-refs/"], "bkt")
    assert photo_uri_errors(["gs://bkt/person-refs/a.jpg"], None)
