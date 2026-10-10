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
    assert "{person_casting_rules?}\n    </INSTRUCTIONS>" in instr, name
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
    assert "real-person, tragedy or crisis trends" in rules
    assert "never in meme, isometric or product-only concepts" in rules
    assert "`casts_person_reference`" in rules and "`person_casting_reason`" in rules
    assert "face is clearly visible" in rules
    assert "keep both fields" in rules


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
    assert "never in meme, isometric or product-only concepts" in illustrated


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
