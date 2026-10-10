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
    concept_prompt,
    main,
    palette_descriptor,
    parse_framings,
    parse_styles,
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


NON_PHOTO = (
    "3D character render",
    "Comic panel",
    "2D flat / vector cartoon",
    "Collage / mixed-media",
)


def test_build_grid_custom_styles_and_framings():
    grid = build_grid(PHOTOS, styles=NON_PHOTO, framings=("mid",))
    assert len(grid) == 8
    assert [g.style for g in grid[:4]] == list(NON_PHOTO)
    assert {g.framing for g in grid} == {"mid"}
    for item in grid:
        assert f"in the style of {item.style}" in item.prompt
        assert "the person in the person reference image" in item.prompt
        assert "café" in item.prompt and "coffee" in item.prompt
        assert "{" not in item.prompt
    assert build_grid(PHOTOS[:1]) == build_grid(
        PHOTOS[:1], styles=STYLES, framings=FRAMINGS
    )


def test_non_photographic_prompt_uses_the_palette_descriptor():
    assert "halftone shading" in palette_descriptor("Comic panel")
    assert palette_descriptor("Not a family") == ""
    prompt = concept_prompt("Comic panel", "close")
    assert "halftone shading" in prompt
    assert "close-up" in prompt.lower()
    # Photographic prompts are unchanged (their own cues, no "in the style of").
    assert "in the style of" not in concept_prompt(STYLES[0], "close")
    # Every family has a descriptor in IMAGE_PROMPT_GUIDE.
    assert all(palette_descriptor(f) for f in ALL_FAMILIES)


def test_parse_styles_and_framings():
    assert parse_styles(None) == STYLES
    assert parse_styles(list(NON_PHOTO)) == NON_PHOTO
    assert parse_styles(["Comic panel", "Comic panel"]) == ("Comic panel",)
    with pytest.raises(ValueError, match="Unknown style family 'Comic'"):
        parse_styles(["Comic"])
    assert parse_framings(None) == FRAMINGS
    assert parse_framings(["mid"]) == ("mid",)
    with pytest.raises(ValueError, match="Unknown framing 'wide'"):
        parse_framings(["wide"])


def test_main_rejects_unknown_style_with_exit_1(capsys, monkeypatch):
    from creative_agent.config import config

    monkeypatch.setattr(config, "GCS_BUCKET_NAME", "b")
    with pytest.raises(SystemExit) as exc:
        main(["--photos", PHOTOS[0], "--styles", "Comic", "--dry-run"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Unknown style family 'Comic'" in err and "Comic panel" in err


def test_main_dry_run_with_styles_and_framings(capsys, monkeypatch):
    from creative_agent.config import config

    monkeypatch.setattr(config, "GCS_BUCKET_NAME", "b")
    main(
        [
            "--photos",
            PHOTOS[0],
            "--styles",
            *NON_PHOTO,
            "--framings",
            "close",
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert "4 concepts x 2 renders = 8 renders" in out
    assert "Comic panel | close" in out


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
    assert "- Styles: A" in md


def test_summarise_lists_the_styles_in_order():
    rows = [_row("B", "close", False), _row("A", "mid", False), _row("B", "mid", False)]
    assert summarise(rows)["styles"] == ["B", "A"]


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
