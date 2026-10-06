"""Deterministic trend-motif + product guard on the final image prompts (Task 4b)."""

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
