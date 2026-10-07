"""Deterministic guards on the final visual concepts: the trend-motif / product /
brand-cue prompt repair (Task 4b) and the concept_gate checks (concept_issues)."""

import pytest

from creative_agent.concept_guard import (
    concept_issues,
    concept_keys,
    ensure_trend_and_product,
    format_concept_issues,
    is_centred_hero,
    is_meme_or_comic,
    restore_unflagged_concepts,
)

PRODUCT = "SE CE24 Electric Guitar"


def _c(prompt, motif="a ballot box"):
    return {
        "concept_name": "x",
        "trend_motif": motif,
        "image_generation_prompt": prompt,
    }


def test_untouched_when_motif_and_product_present():
    c = _c("A cut-paper collage of an SE CE24 Electric Guitar beside a ballot box.")
    out, warns = ensure_trend_and_product([c], PRODUCT)
    assert out[0]["image_generation_prompt"] == c["image_generation_prompt"]
    assert warns == []


def test_appends_missing_motif():
    out, warns = ensure_trend_and_product(
        [_c("A studio photo of an SE CE24 Electric Guitar.")], PRODUCT
    )
    assert "a ballot box" in out[0]["image_generation_prompt"]
    assert any("trend_motif" in w for w in warns)


def test_appends_missing_product():
    out, warns = ensure_trend_and_product(
        [_c("A watercolor of a ballot box in a plaza.")], PRODUCT
    )
    assert PRODUCT in out[0]["image_generation_prompt"]
    assert any("product" in w for w in warns)


def test_case_insensitive_and_empty_motif_only_warns():
    out, _ = ensure_trend_and_product(
        [_c("an se ce24 electric guitar and A BALLOT BOX")], PRODUCT
    )
    # Already present (case-insensitively): nothing appended, no duplicate.
    assert out[0]["image_generation_prompt"].lower().count("ballot") == 1
    out, warns = ensure_trend_and_product(
        [_c("an SE CE24 Electric Guitar", motif="")], PRODUCT
    )
    assert any("empty trend_motif" in w for w in warns)


def test_input_not_mutated():
    c = _c("A studio photo.")
    ensure_trend_and_product([c], PRODUCT)
    assert c["image_generation_prompt"] == "A studio photo."


# --- brand_cue repair -------------------------------------------------------


def test_appends_missing_brand_cue_case_insensitively():
    c = {
        **_c(f"A studio photo of an {PRODUCT} beside a ballot box."),
        "brand_cue": "PRS bird inlays",
    }
    out, warns = ensure_trend_and_product([c], PRODUCT)
    prompt = out[0]["image_generation_prompt"]
    assert prompt.endswith(" The scene features PRS bird inlays.")
    assert any("brand_cue" in w for w in warns)
    # Already present (case-insensitively), or no cue: nothing appended.
    for cue, text in (("PRS bird inlays", "prs BIRD inlays"), ("", "")):
        c = {
            **_c(f"An {PRODUCT} with {text} beside a ballot box."),
            "brand_cue": cue,
        }
        out, warns = ensure_trend_and_product([c], PRODUCT)
        assert out[0]["image_generation_prompt"] == c["image_generation_prompt"]
        assert warns == []


# --- concept_issues ---------------------------------------------------------

COPIES = {
    "ad_copies": [
        {
            "original_id": i,
            "headline": f"Beep Beep, Coyote {i}!",
            "call_to_action": "Order yours today",
        }
        for i in range(1, 5)
    ]
}


def _concept(ad_copy_id, prompt, motif="a roadrunner dust cloud", **extra):
    return {
        "ad_copy_id": ad_copy_id,
        "concept_name": f"C{ad_copy_id}",
        "trend_motif": motif,
        "image_generation_prompt": prompt,
        **extra,
    }


def _issue_texts(issues, key):
    return [i.text for i in issues[key]]


def test_concept_keys_follow_ad_copy_id_and_dedupe():
    concepts = [_concept(1, "a"), _concept(1, "b"), {"image_generation_prompt": "c"}]
    assert concept_keys(concepts) == ["1", "1#2", "#2"]


def test_quoted_text_matching_the_paired_copy_passes():
    concepts = [
        # Exact headline, straight quotes.
        _concept(1, 'Bold type reads "Beep Beep, Coyote 1!" at the bottom.'),
        # Curly quotes, case/punctuation/whitespace differences, the CTA.
        _concept(2, "A sticker that says “ORDER  yours today”."),
    ]
    assert concept_issues(concepts, COPIES) == {}


def test_quoted_text_substring_either_way_passes():
    concepts = [
        _concept(1, 'Type reads "Beep beep".'),  # quote inside the headline
        _concept(2, 'Type reads "Beep Beep, Coyote 2! Order yours today".'),
    ]
    assert concept_issues(concepts, COPIES) == {}


def test_quoted_text_not_from_the_copy_is_flagged():
    concepts = [_concept(3, "A neon sign that reads “Speed is life”.")]
    issues = concept_issues(concepts, COPIES)
    (text,) = _issue_texts(issues, "3")
    assert text.startswith('in-image text "Speed is life" is not the paired')
    assert '"Beep Beep, Coyote 3!" or "Order yours today"' in text
    assert all(i.kind == "deterministic" for i in issues["3"])


def test_meme_and_comic_prompts_are_exempt():
    concepts = [
        _concept(1, 'Impact meme caption reads "When the skates finally work".'),
        _concept(2, 'A comic panel; the speech bubble says "Meep meep?".'),
        _concept(
            3,
            'A reaction image, top caption "me vs the roadrunner".',
            visual_style="Meme aesthetic",
        ),
    ]
    # Exempt from the quote match AND from the text cap.
    assert concept_issues(concepts, COPIES) == {}


def test_exemption_by_visual_style_family_without_keywords():
    concept = _concept(
        1, 'Bold text reads "Nope, not today".', visual_style="meme AESTHETIC (wojak)"
    )
    assert concept_issues([concept], COPIES) == {}
    comic = _concept(2, 'Text reads "Kapow".', visual_style="Comic panel")
    assert concept_issues([comic], COPIES) == {}


def test_exempt_style_families_exist_in_the_style_palette():
    from creative_agent.concept_guard import EXEMPT_STYLE_FAMILIES
    from creative_agent.style_shortlist import STYLE_GROUPS

    families = {name for names in STYLE_GROUPS.values() for name in names}
    assert set(EXEMPT_STYLE_FAMILIES) <= families


def test_bare_caption_or_meme_words_do_not_exempt():
    # The guide's own negative-space phrase must not switch the checks off.
    concept = _concept(
        3,
        "Leave clean negative space for the platform's own caption. "
        'A neon sign reading "Speed is life".',
        visual_style="Photoreal / editorial",
    )
    issues = concept_issues([concept], COPIES)
    assert _issue_texts(issues, "3")[0].startswith('in-image text "Speed is life"')


# --- brand / product quotes (item 1) ----------------------------------------


def test_quoted_brand_logo_is_allowed_and_not_counted():
    concepts = [
        _concept(1, 'Type reads "Beep Beep, Coyote 1!".'),
        _concept(2, 'Type reads "Order yours today".'),
        _concept(3, 'The headstock bears the "PRS" logo, text reads "PRS".'),
    ]
    assert concept_issues(concepts, COPIES, brand="PRS") == {}


def test_quoted_product_and_brand_cue_are_allowed():
    concepts = [
        _concept(1, 'Lettering reads "SE CE24".'),
        _concept(2, 'A sign reading "bird inlays".', brand_cue="PRS bird inlays"),
        _concept(3, 'Type reads "Beep Beep, Coyote 3!".'),
        _concept(4, 'Type reads "Order yours today".'),
    ]
    assert concept_issues(concepts, COPIES, target_product=PRODUCT) == {}


def test_short_brand_does_not_whitelist_unrelated_words():
    concept = _concept(3, 'A sign reading "Great gear".')
    issues = concept_issues([concept], COPIES, brand="GE")
    assert _issue_texts(issues, "3")[0].startswith('in-image text "Great gear"')


# --- descriptive quotes (item 4) --------------------------------------------


def test_descriptive_quotes_without_a_text_cue_are_ignored():
    concepts = [
        _concept(1, 'Bathed in "golden hour" light, a "Cinematic film still".'),
        _concept(2, 'A "Retro / vaporwave" palette.'),
        _concept(3, 'Rendered as "Diecut sticker" art.'),
    ]
    # Neither mismatch nor cap.
    assert concept_issues(concepts, COPIES) == {}


def test_background_sign_text_is_flagged():
    issues = concept_issues([_concept(1, 'A neon sign reading "Open".')], COPIES)
    assert _issue_texts(issues, "1")[0].startswith('in-image text "Open"')


def test_headline_quote_with_trailing_punctuation_matches():
    copies = {"ad_copies": [{"original_id": 1, "headline": "Outrun Monday"}]}
    concept = _concept(1, 'The headline text reads "Outrun Monday." in bold.')
    assert concept_issues([concept], copies) == {}


def test_in_image_quotes_need_a_cue_within_three_words():
    from creative_agent.concept_guard import in_image_quotes

    assert in_image_quotes('A sign reading "Open"') == ["Open"]
    assert in_image_quotes('A SLOGAN: "Go"') == ["Go"]
    assert in_image_quotes('Bathed in "golden hour" light') == []
    far = 'The text sits low and the light is warm and soft "Hi"'
    assert in_image_quotes(far) == []


def test_unpaired_concept_falls_back_to_its_own_fields_or_is_skipped():
    own = _concept(
        9,
        'Type reads "Zoom zoom".',
        headline="Zoom zoom",
        call_to_action="Shop now",
    )
    assert concept_issues([own], COPIES) == {}
    bare = _concept(9, 'Type reads "Anything at all".')
    assert concept_issues([bare], COPIES) == {}
    assert concept_issues([bare], None) == {}


def test_empty_trend_motif_is_flagged():
    issues = concept_issues([_concept(1, "A watercolor.", motif="  ")], COPIES)
    (text,) = _issue_texts(issues, "1")
    assert text.startswith("trend_motif is empty")


def test_text_cap_flags_only_the_extra_text_concepts():
    concepts = [
        _concept(1, 'Type reads "Beep Beep, Coyote 1!".'),
        _concept(2, "No text, clean negative space."),
        _concept(3, 'Type reads "Order yours today".'),
        _concept(4, 'Type reads "Beep Beep, Coyote 4!".'),
    ]
    issues = concept_issues(concepts, COPIES)
    assert list(issues) == ["4"]
    (text,) = _issue_texts(issues, "4")
    assert text.startswith("in-image text appears in more than 2 concepts")
    assert concept_issues(concepts, COPIES, max_text_concepts=3) == {}


def test_centred_hero_flags_every_extra_one():
    concepts = [
        _concept(1, "A centred hero shot of the skates on a mesa."),
        _concept(2, "Off-centre rule-of-thirds framing of a coyote."),
        _concept(3, "The skates sit dead center against teal."),
        _concept(4, "A symmetrical hero composition, centered skates."),
    ]
    issues = concept_issues(concepts, COPIES)
    assert list(issues) == ["3", "4"]
    for key in ("3", "4"):
        (text,) = _issue_texts(issues, key)
        assert text.startswith("more than one centred hero")


def test_centred_hero_heuristic_is_conservative():
    assert is_centred_hero("A centered hero shot.")
    assert is_centred_hero("Center-framed skates")
    assert not is_centred_hero("An off-centered subject, off-center light.")
    # A sentence about text or logo placement is not a hero composition.
    assert not is_centred_hero("The headline is centred at the bottom.")
    assert not is_centred_hero('A sign "dead center" hangs on the wall.')
    assert not is_centred_hero("A wide desert road at dusk.")


def test_centred_hero_ignores_negations_and_small_in_wide_framing():
    assert not is_centred_hero("The skates are not centred; rule of thirds.")
    assert not is_centred_hero("Not centered, the coyote sits left.")
    assert not is_centred_hero("Avoid centring: an off-center subject.")
    assert not is_centred_hero("Avoid a centred hero shot.")
    assert not is_centred_hero("A skater small in a wide, centred desert frame.")
    assert is_centred_hero("A centred hero shot of the skates.")


def test_in_image_quotes_ignores_letterless_quotes():
    from creative_agent.concept_guard import in_image_quotes

    text = 'Text reads "42" and "Go fast" and “Zoom”'
    assert in_image_quotes(text) == ["Go fast", "Zoom"]


def test_concept_issues_tolerates_bad_input():
    for value in (None, "", "not json", {"visual_concepts": "x"}, [1, "a"]):
        assert concept_issues(value, COPIES) == {}
    as_json = '{"visual_concepts": [{"ad_copy_id": 1, "trend_motif": ""}]}'
    assert list(concept_issues(as_json, "not json")) == ["1"]


def test_format_concept_issues_groups_per_concept():
    concepts = [_concept(2, "x", motif="")]
    text = format_concept_issues(concepts, concept_issues(concepts, COPIES))
    assert text.splitlines()[0] == '- **Concept 2 ("C2"):**'
    assert text.splitlines()[1].startswith("  - trend_motif is empty")


def test_restore_unflagged_concepts_only_takes_flagged_ones():
    before = {"visual_concepts": [_concept(1, "one"), _concept(2, "two")]}
    after = {
        "visual_concepts": [
            _concept(1, "one, rewritten unasked"),
            _concept(2, "two, fixed"),
            _concept(7, "invented"),
        ]
    }
    restored, notes = restore_unflagged_concepts(before, after, ["2"])
    assert [c["image_generation_prompt"] for c in restored["visual_concepts"]] == [
        "one",
        "two, fixed",
    ]
    assert "restored concept 1: it was not flagged for revision" in notes
    assert "dropped concept 7: not in the pre-revision concepts" in notes
    # Nothing usable before: the fixer's output is returned as is.
    restored, notes = restore_unflagged_concepts(None, after, ["2"])
    assert len(restored["visual_concepts"]) == 3 and notes == []


# --- false-positive regressions (concept_gate audit) ------------------------

AUDIT_COPIES = [
    {"original_id": str(i), "headline": h, "call_to_action": c}
    for i, (h, c) in enumerate(
        [
            ("Play Your Era.", "Shop the SE CE24"),
            ("Every Fret, Your Story", "Find yours"),
            ("Built to be heard", "Learn more"),
            ("Own the stage!", "Shop now"),
        ],
        1,
    )
]
AUDIT_BRAND = "PRS Guitars"
AUDIT_PRODUCT = "PRS SE CE24 electric guitar"


def _audit(i, prompt, style="Editorial photography"):
    return _concept(str(i), prompt, motif="friendship bracelets", visual_style=style)


@pytest.mark.parametrize(
    "prompt",
    [
        'The vibe reads "effortless" and loose.',
        'The mood says "calm" all over.',
        'A sign of the times: "Y2K revival" fashion everywhere.',
        'The title of the song "Cruel Summer" inspires the colour palette.',
        'Poster style, "Wes Anderson" symmetry.',
        'banner of light, "aurora" streaks',
        'a label-free bottle, "matte black" finish',
        'A title-card mood, "golden hour" haze.',
        'words cannot capture the "wow" moment',
    ],
)
def test_idioms_and_loose_cues_are_not_in_image_text(prompt):
    from creative_agent.concept_guard import in_image_quotes

    assert in_image_quotes(prompt) == []


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ('A neon sign reading "Open"', ["Open"]),
        ('The headline text reads "Outrun Monday." in bold.', ["Outrun Monday."]),
        ('small caption "Find yours"', ["Find yours"]),
        ('Bold lettering: "Shop the SE CE24"', ["Shop the SE CE24"]),
        ('Sign reading "Play Your Era" and "Shop now"', ["Play Your Era", "Shop now"]),
        ('A sign reading "Open" beside a "golden hour" glow', ["Open"]),
    ],
)
def test_real_in_image_text_is_still_found(prompt, expected):
    from creative_agent.concept_guard import in_image_quotes

    assert in_image_quotes(prompt) == expected


def test_idiom_quotes_are_neither_mismatch_nor_capped():
    concepts = [
        _audit(1, 'Neon sign reading "PLAY YOUR ERA"'),
        _audit(2, 'Chalkboard sign reading "Find yours"'),
        _audit(3, 'The vibe reads "effortless".'),
        _audit(4, 'Poster style, "Wes Anderson" palette'),
    ]
    assert (
        concept_issues(
            concepts, AUDIT_COPIES, brand=AUDIT_BRAND, target_product=AUDIT_PRODUCT
        )
        == {}
    )


def test_mismatch_concept_does_not_push_a_later_one_over_the_cap():
    concepts = [
        _audit(1, 'Sign reading "Play Your Era"'),
        _audit(2, 'A neon sign reading "Speed is life"'),  # mismatch
        _audit(3, 'Poster says "Learn more"'),
    ]
    issues = concept_issues(
        concepts, AUDIT_COPIES, brand=AUDIT_BRAND, target_product=AUDIT_PRODUCT
    )
    assert list(issues) == ["2"]
    (text,) = _issue_texts(issues, "2")
    assert text.startswith('in-image text "Speed is life"')


def test_cap_still_counts_copy_matching_text_concepts():
    concepts = [
        _audit(1, 'Neon sign reading "Play Your Era." over a stage.'),
        _audit(2, 'Headline text "Every Fret, Your Story" in the sky.'),
        _audit(3, 'Poster says "Built to be heard!"'),
        _audit(4, "clean shot"),
    ]
    issues = concept_issues(
        concepts, AUDIT_COPIES, brand=AUDIT_BRAND, target_product=AUDIT_PRODUCT
    )
    assert list(issues) == ["3"]
    assert _issue_texts(issues, "3")[0].startswith("in-image text appears in more")


@pytest.mark.parametrize(
    ("prompt", "style"),
    [
        ('Impact-font top text reading "WHEN THE SOLO HITS"', "Lo-fi internet humour"),
        ('Text reads "nope"', "Lo-fi MEME energy"),
        ('Bottom text reads "me at 3am"', "Editorial photography"),
        ('Impact font caption reads "same"', "Editorial photography"),
        ('A speech balloon says "Again?"', "Graphic novel"),
        ('A thought bubble reads "one more song"', "Graphic novel"),
    ],
)
def test_wider_meme_and_comic_exemption(prompt, style):
    assert is_meme_or_comic(prompt, style)


def test_impact_font_meme_concept_is_exempt_from_mismatch_and_cap():
    concepts = [
        _audit(1, 'Sign reading "Play Your Era"'),
        _audit(2, 'Text "Find yours"'),
        _audit(
            3,
            'Impact-font top text reading "WHEN THE SOLO HITS"',
            style="Lo-fi internet humour",
        ),
        _audit(4, ""),
    ]
    assert (
        concept_issues(
            concepts, AUDIT_COPIES, brand=AUDIT_BRAND, target_product=AUDIT_PRODUCT
        )
        == {}
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "Subject centred in the lower third of the frame.",
        "Subject centred in the lower third.",
        "The bracelet is centered between two hands.",
        "Camera centered on the crowd, the guitarist small at the left.",
        "The lamp light is centred.",
    ],
)
def test_centring_phrases_that_are_not_a_centred_hero(prompt):
    assert not is_centred_hero(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "Centered hero composition of the guitar.",
        "Centered composition, guitar hero shot.",
        "Symmetrical hero framing, centered.",
        "A centred, symmetrical Wes Anderson framing of the shop.",
        "Center-framed portrait.",
        "The skates sit dead center against teal.",
    ],
)
def test_centred_hero_still_detected(prompt):
    assert is_centred_hero(prompt)
