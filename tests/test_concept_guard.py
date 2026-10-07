"""Deterministic trend-motif + product guard on the final image prompts (Task 4b)."""

import pytest

from creative_agent.concept_guard import ensure_trend_and_product

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


# --- Token-overlap matching (false-positive audit, 80 realistic prompts) -----


def _prompt_after(prompt, product, motif, brand=""):
    out, warns = ensure_trend_and_product([_c(prompt, motif)], product, brand=brand)
    return out[0]["image_generation_prompt"][len(prompt) :], warns


def test_motif_paraphrase_with_hyphen_and_plural_not_appended():
    tail, warns = _prompt_after(
        "A guitarist's wrist with a friendship-bracelet stack under Eras Tour "
        "lights, PRS SE CE24 electric guitar on lap.",
        "PRS SE CE24 electric guitar",
        "Eras Tour friendship bracelets",
    )
    assert tail == "" and warns == []


def test_motif_without_its_trend_context_is_appended():
    # Documented decision: "friendship-bracelet stack" alone covers 2 of the
    # motif's 4 content tokens (< 60%), so the Eras Tour context is appended.
    tail, _ = _prompt_after(
        "A wrist with a friendship-bracelet stack and a PRS SE CE24 electric guitar.",
        "PRS SE CE24 electric guitar",
        "Eras Tour friendship bracelets",
    )
    assert tail == " The scene visibly includes Eras Tour friendship bracelets."


@pytest.mark.parametrize(
    ("product", "prompt"),
    [
        (
            "PRS SE CE24 electric guitar",
            "Wrist stacked with friendship bracelets beside a PRS SE guitar on a stand.",
        ),
        (
            "Liquid Death Mountain Water 16oz can",
            "An ice-cold Liquid Death Mountain Water tallboy can beside friendship bracelets.",
        ),
        (
            "Oreo Double Stuf cookies",
            "A twisted-open Oreo Double Stuf cookie next to friendship bracelets.",
        ),
    ],
)
def test_product_paraphrase_not_appended(product, prompt):
    tail, warns = _prompt_after(prompt, product, "friendship bracelets")
    assert tail == "" and warns == []


@pytest.mark.parametrize("product", ["Patagonia Nano Puff Jacket", "Nano Puff jacket"])
def test_generic_noun_alone_still_appends_product(product):
    tail, warns = _prompt_after(
        "A lone hiker in a sleek insulated jacket under green aurora ribbons.",
        product,
        "green aurora ribbons",
    )
    assert tail == f" The {product} is clearly visible and recognizable."
    assert any("product missing" in w for w in warns)


def test_intangible_product_gets_depictable_line():
    tail, warns = _prompt_after(
        "Fans raise knit scarves, forming stadium scarf walls.",
        "Max subscription",
        "stadium scarf walls",
    )
    assert "clearly visible" not in tail
    assert (
        tail
        == " The Max subscription is suggested through a branded app screen or logo in the scene."
    )
    assert any("product missing" in w for w in warns)


def test_intangible_product_skipped_when_brand_mentioned():
    tail, warns = _prompt_after(
        "Duo the green Duolingo owl raises vuvuzela-style horns.",
        "Duolingo Max subscription",
        "vuvuzela-style horns",
        brand="Duolingo",
    )
    assert tail == "" and warns == []


def test_tangible_product_not_skipped_by_brand_alone():
    tail, _ = _prompt_after(
        "A Patagonia flag over green aurora ribbons.",
        "Patagonia Nano Puff Jacket",
        "green aurora ribbons",
        brand="Patagonia",
    )
    assert "clearly visible" in tail


def test_product_mention_must_carry_the_brand_when_the_product_names_it():
    tail, warns = _prompt_after(
        "An iced coffee sweating on a cafe table under festival bunting.",
        "Starbucks iced coffee",
        "festival bunting",
        brand="Starbucks",
    )
    assert tail == " The Starbucks iced coffee is clearly visible and recognizable."
    assert any("product missing" in w for w in warns)
    tail, warns = _prompt_after(
        "A Starbucks iced latte on a cafe table under festival bunting.",
        "Starbucks iced coffee",
        "festival bunting",
        brand="Starbucks",
    )
    assert tail == "" and warns == []


def test_two_word_packaging_product_needs_its_head_noun():
    tail, _ = _prompt_after(
        "An iPhone on a desk beside a ballot box.", "iPhone case", "a ballot box"
    )
    assert tail == " The iPhone case is clearly visible and recognizable."
