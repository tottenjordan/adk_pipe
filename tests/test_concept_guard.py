"""Deterministic guards on the final visual concepts: the trend-motif / product /
brand-cue prompt repair (Task 4b) and the concept_gate checks (concept_issues)."""

from creative_agent.concept_guard import (
    concept_issues,
    concept_keys,
    ensure_trend_and_product,
    format_concept_issues,
    is_centred_hero,
    quoted_texts,
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
        _concept(1, 'A meme with an Impact caption: "When the skates finally work".'),
        _concept(2, 'A comic panel; the speech bubble says "Meep meep?".'),
        _concept(3, 'A reaction image, top caption "me vs the roadrunner".'),
    ]
    # Exempt from the quote match AND from the text cap.
    assert concept_issues(concepts, COPIES) == {}


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


def test_quoted_texts_ignores_letterless_quotes():
    assert quoted_texts('Type "42" and "Go fast" and “Zoom”') == ["Go fast", "Zoom"]


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
