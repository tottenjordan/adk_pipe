"""Prompt wiring for the visual concept stage (brand cue, copy-quoted text, brief).

Every concept carries a `brand_cue` (a brand distinctive asset from the brief or
the user's brand colours) named in its image prompt; in-image text is quoted
exactly from the paired copy's headline or CTA (meme captions / comic speech
bubbles excepted); the brief's avoid list and fit_mode are respected; the
finalizer carries the paired copy's `angle_id`. Also guards that every `{...}`
is a well-formed ADK state token and that the shared rules are brace-free.
"""

import re

import pytest

from creative_agent import prompts

VISUAL_PROMPTS = [
    "VISUAL_CONCEPT_DRAFTER_INSTR",
    "VISUAL_CONCEPT_CRITIC_INSTR",
    "VISUAL_CONCEPT_FINALIZER_INSTR",
]
_TOKEN = re.compile(r"\{([^{}]*)\}")


def test_shared_rules_are_brace_free():
    for constant in (
        prompts.VISUAL_BRAND_CUE_RULE,
        prompts.VISUAL_TEXT_FROM_COPY_RULE,
        prompts.VISUAL_BRIEF_LIMITS_RULE,
        prompts.VISUAL_MOTIF_PROMINENCE_RULE,
        prompts.VISUAL_CONCEPT_RULES,
        prompts.IMAGE_PROMPT_GUIDE,
    ):
        assert "{" not in constant and "}" not in constant


def test_shared_rules_wording():
    assert "`brand_cue`" in prompts.VISUAL_BRAND_CUE_RULE
    assert "brand distinctive assets" in prompts.VISUAL_BRAND_CUE_RULE
    assert "<user_brand_colors>" in prompts.VISUAL_BRAND_CUE_RULE
    assert "verbatim into `image_generation_prompt`" in prompts.VISUAL_BRAND_CUE_RULE
    text = prompts.VISUAL_TEXT_FROM_COPY_RULE
    assert "`headline` or `call_to_action`" in text
    assert "copied exactly inside double quotes" in text
    assert "meme caption" in text and "speech bubble" in text
    limits = prompts.VISUAL_BRIEF_LIMITS_RULE
    assert "avoid list" in limits and "<user_avoid>" in limits
    assert "light_touch = the trend shows only as mood, motif or format" in limits
    assert "never forced into the trend scene" in limits
    # Aligned with the judge's trend_motif_visible gate (prominent, at a glance).
    motif = prompts.VISUAL_MOTIF_PROMINENCE_RULE
    assert "prominent" in motif and "recognisable at a glance" in motif
    assert "never tiny or only in the background" in motif
    assert motif in prompts.VISUAL_CONCEPT_RULES


@pytest.mark.parametrize("name", VISUAL_PROMPTS)
def test_visual_prompts_carry_the_shared_rules_and_inputs(name):
    instr = getattr(prompts, name)
    assert prompts.VISUAL_CONCEPT_RULES in instr, name
    # The blocks the rules refer to are all present (and optional tokens).
    for token in (
        "{creative_brief_md?}",
        "{brand_colors?}",
        "{visual_avoid?}",
        "{ad_copy_critique?}",
    ):
        assert token in instr, (name, token)
    assert "`brand_cue`" in instr and "`angle_id`" in instr, name


def test_finalizer_takes_angle_id_from_the_paired_copy():
    instr = prompts.VISUAL_CONCEPT_FINALIZER_INSTR
    assert "use its exact `headline`, `social_caption`, `call_to_action` and" in instr
    assert "Set `angle_id` to the matching ad copy's `angle_id`" in instr
    assert prompts.VISUAL_CRITIC_BRIEF_RULE in instr
    assert instr.count(prompts.BRIEF_BLOCK) == 1


def test_guide_quotes_in_image_text_from_the_copy():
    guide = prompts.IMAGE_PROMPT_GUIDE
    assert "the paired ad copy's headline OR call-to-action, copied exactly" in guide
    assert "may be new short text" in guide


@pytest.mark.parametrize("name", VISUAL_PROMPTS)
def test_every_brace_is_a_state_token(name):
    instr = getattr(prompts, name)
    for token in _TOKEN.findall(instr):
        assert re.fullmatch(r"[A-Za-z_]\w*\??", token), (name, token)
    stripped = _TOKEN.sub("", instr)
    assert "{" not in stripped and "}" not in stripped, name


@pytest.mark.parametrize("name", VISUAL_PROMPTS)
def test_only_seeded_campaign_tokens_are_required(name):
    """Required (non-`?`) tokens are only the campaign fields the state init
    always setdefaults; everything else is optional."""
    required = {t for t in _TOKEN.findall(getattr(prompts, name)) if "?" not in t}
    assert required <= {
        "brand",
        "target_product",
        "target_audience",
        "target_search_trends",
        "key_selling_points",
    }, name


# --- Person casting: only runs with a person reference see the rules ---------------


def _person_prompts():
    from interactive_creative import prompts as ic_prompts

    return {
        "ART_DIRECTOR_INSTR": prompts.ART_DIRECTOR_INSTR,
        **{name: getattr(prompts, name) for name in VISUAL_PROMPTS},
        "VISUAL_CONCEPT_FIXER_INSTR": prompts.VISUAL_CONCEPT_FIXER_INSTR,
        "VISUAL_CONCEPT_REVISER_INSTR": ic_prompts.VISUAL_CONCEPT_REVISER_INSTR,
    }


_PERSON_WORDS = (
    "person",
    "casts_person_reference",
    "person_casting_reason",
    "cast",
)


@pytest.mark.parametrize("name", list(_person_prompts()))
def test_casting_rules_come_only_from_state(name):
    """Without a person the token resolves to "", leaving the instruction free of
    any casting text (byte-identical to before the feature)."""
    instr = _person_prompts()[name]
    assert instr.count("{person_casting_rules?}") == 1, name
    # Last in the instructions, followed only by the timely slot's (and the
    # rating limits' and the safer angle's) own tokens.
    assert re.search(
        r"\{person_casting_rules\?\}\{timely_rules\?\}(\{rating_limits\?\})?"
        r"\{safer_angle_visual_rules\?\}\n    </INSTRUCTIONS>",
        instr,
    ), name
    without = instr.replace("{person_casting_rules?}", "").lower()
    for word in _PERSON_WORDS:
        assert f"`{word}`" not in without, (name, word)
    assert "person reference" not in without, name
    assert prompts.PERSON_CASTING_RULES not in instr


def test_shared_rules_and_guide_have_no_casting_text():
    for constant in (prompts.VISUAL_CONCEPT_RULES, prompts.IMAGE_PROMPT_GUIDE):
        assert "person reference" not in constant.lower()
        assert "casts_person_reference" not in constant


def test_person_casting_rules_wording():
    from creative_agent.config import config

    rules = prompts.PERSON_CASTING_RULES
    assert "{" not in rules and "}" not in rules
    assert rules.startswith("\n\n    **Person casting")
    assert f"at most {config.max_cast_concepts} of the 4 concepts" in rules
    for style in config.person_safe_styles:
        assert style in rules, style
    assert "the person in the person reference image" in rules
    assert "name the hero's role" in rules
    assert "never their face, age, ethnicity or body" in rules
    assert "real-person, tragedy or crisis trends" not in rules
    assert "Never cast in meme, isometric or product-only concepts" in rules
    assert "`casts_person_reference`" in rules and "`person_casting_reason`" in rules
    assert "face is clearly visible" in rules
    assert "keep both fields" in rules


def test_person_casting_rules_allow_real_person_and_sensitive_trends():
    """Product decision (2026-10-12): casting is allowed on real-person and
    sensitive trends, with identity guardrails; the sensitive event itself is
    still never referenced."""
    rules = prompts.PERSON_CASTING_RULES
    assert "Never cast for" not in rules
    assert "Casting is allowed when the trend is about a real person" in rules
    assert "never a likeness of the trending real person" in rules
    assert "appears as themselves" in rules
    assert "never presented as the real person" in rules
    assert "costume, signature attire, uniform or vestments" in rules
    assert "endorsed by them" in rules
    assert "never place the cast person in a scene depicting" in rules
    assert "injury, damage or victims" in rules
    assert "general cultural iconography" in rules
    assert "never reference the sensitive event itself" in rules
    for style_set in (PHOTOGRAPHIC, ("Comic panel",)):
        built = prompts.build_person_casting_rules(style_set, 2)
        assert "{" not in built and "}" not in built
        assert "appears as themselves" in built


def test_person_casting_rules_keep_the_product_out_of_the_persons_hands():
    """2026-10-13: cast concepts failed the judge's no_visual_defects gate on
    malformed hands 67% of the time when the person handled the product (36%
    without), so the rules keep the product beside the person."""
    for rules in (
        prompts.PERSON_CASTING_RULES,
        prompts.build_person_casting_rules(PHOTOGRAPHIC, 2),
    ):
        assert "{" not in rules and "}" not in rules
        assert "never grip, clink, pour or hold the product up" in rules
        assert "place the product beside them" in rules
        assert "hands relaxed or partly out of frame" in rules


ILLUSTRATED_LINE = (
    "In an illustrated style, keep the person's distinctive features recognisable "
    "(face shape, hairline, hair colour, skin tone, facial hair, glasses) rather "
    "than a generic cartoon face."
)
PHOTOGRAPHIC = (
    "Candid 35mm film photo",
    "Photoreal / editorial",
    "Cinematic film still",
)


def test_person_casting_rules_illustrated_line_only_with_illustrated_styles():
    photographic = prompts.build_person_casting_rules(PHOTOGRAPHIC, 2)
    assert ILLUSTRATED_LINE not in photographic
    assert "comic" not in photographic.lower()
    illustrated = prompts.build_person_casting_rules(
        ("Photoreal / editorial", "Comic panel"), 1
    )
    assert ILLUSTRATED_LINE in illustrated
    assert "{" not in illustrated and "}" not in illustrated
    assert "at most 1 of the 4 concepts" in illustrated
    assert "is one of: Photoreal / editorial; Comic panel, and" in illustrated
    assert "Never cast in meme, isometric or product-only concepts" in illustrated


def test_default_person_casting_rules_include_the_illustrated_line():
    from creative_agent.config import config

    assert ILLUSTRATED_LINE in prompts.PERSON_CASTING_RULES
    assert prompts.PERSON_CASTING_RULES == prompts.build_person_casting_rules(
        config.person_safe_styles, config.max_cast_concepts
    )


def test_interactive_reviser_keeps_brand_cue_and_angle_id():
    from interactive_creative import prompts as ic_prompts

    instr = ic_prompts.VISUAL_CONCEPT_REVISER_INSTR
    assert "`trend_motif` and `brand_cue` words in the rewritten prompt" in instr
    assert "keep its `angle_id`" in instr


def test_guide_tone_mapping_agrees_with_the_text_rules_and_style_names():
    guide = prompts.IMAGE_PROMPT_GUIDE
    # In-image text is only the paired headline / CTA: no labelled callouts.
    assert "labelled callout" not in guide
    assert "with no labels or callouts" in guide
    # Mapped styles use the palette's family names.
    assert "-> Cinematic film still or Photoreal / editorial" in guide
    assert "-> cinematic photoreal" not in guide


def test_interactive_reviser_carries_the_shared_rules_and_inputs():
    from interactive_creative import prompts as ic_prompts

    instr = ic_prompts.VISUAL_CONCEPT_REVISER_INSTR
    assert prompts.VISUAL_CONCEPT_RULES in instr
    for token in (
        "{creative_brief_md?}",
        "{ad_copy_critique?}",
        "{brand_colors?}",
        "{visual_avoid?}",
        "{target_product}",
    ):
        assert token in instr, token
    for token in _TOKEN.findall(instr):
        assert re.fullmatch(r"[A-Za-z_]\w*\??", token), token
    assert instr.rstrip().endswith(
        "schema (a `visual_concepts` list).**\n    </OUTPUT_FORMAT>"
    )


@pytest.mark.parametrize("name", VISUAL_PROMPTS)
def test_style_preference_exception_reads_the_same_everywhere(name):
    # The 4-distinct-families rule and a user style preference never conflict:
    # every visual stage keeps the preferred family and varies the rest.
    instr = getattr(prompts, name)
    assert "overrides the shortlist" not in instr
    if name != "VISUAL_CONCEPT_CRITIC_INSTR":
        assert "<user_style_preference> is non-empty, keep that family" in instr


def test_drafter_pairs_by_original_id_and_counts_any_centred_hero():
    instr = prompts.VISUAL_CONCEPT_DRAFTER_INSTR
    assert "Set `ad_copy_id` to the paired ad copy's `original_id`" in instr
    # concept_guard counts every centred hero, not only product heroes.
    assert "at most ONE centred hero per set" in instr
    assert "centred product hero" not in instr


def test_visual_style_examples_are_palette_names():
    from creative_agent.schemas import VisualConcept
    from creative_agent.style_shortlist import ALL_FAMILIES

    description = VisualConcept.model_fields["visual_style"].description or ""
    examples = re.findall(r"'([^']+)'", description)
    assert examples and all(e in ALL_FAMILIES for e in examples), examples
