"""creative_agent/concept_guard.enforce_person_casting: the deterministic
person-casting guard, and its wiring into ensure_trend_and_product_callback."""

from __future__ import annotations

import json
from types import SimpleNamespace

from creative_agent.concept_guard import PERSON_HERO_PHRASE, enforce_person_casting

SAFE = frozenset(
    {"Candid 35mm film photo", "Photoreal / editorial", "Cinematic film still"}
)
CAST_PROMPT = (
    "A candid 35mm film photo of a skater, the person in the person reference image laughing "
    "on a rooftop at dusk, holding Rocket Skates."
)


def _concept(i: int, *, cast=True, style="Candid 35mm film photo", prompt=CAST_PROMPT):
    return {
        "ad_copy_id": i,
        "concept_name": f"C{i}",
        "visual_style": style,
        "image_generation_prompt": prompt,
        "casts_person_reference": cast,
        "person_casting_reason": "One hero, face visible." if cast else "",
    }


def _run(concepts, *, available=True, max_cast=2, safe=SAFE):
    return enforce_person_casting(
        concepts, available=available, max_cast=max_cast, safe_styles=safe
    )


def _casts(concepts):
    return [c["casts_person_reference"] for c in concepts]


def test_valid_cast_is_kept_unchanged():
    concepts = [_concept(1), _concept(2, cast=False)]
    out, warnings = _run(concepts)
    assert out == concepts and warnings == []


def test_no_reference_available_clears_every_cast():
    out, warnings = _run([_concept(1), _concept(2)], available=False)
    assert _casts(out) == [False, False]
    assert len(warnings) == 2
    assert "no person reference" in out[0]["person_casting_reason"]
    # The dangling reference wording is neutralised.
    assert PERSON_HERO_PHRASE not in out[0]["image_generation_prompt"].lower()
    assert "a person laughing" in out[0]["image_generation_prompt"]


def test_unsafe_style_clears_the_cast():
    for style in ("Meme aesthetic", "Comic panel", "Isometric miniature world", ""):
        out, warnings = _run([_concept(1, style=style)])
        assert _casts(out) == [False], style
        assert "style" in out[0]["person_casting_reason"], style
        assert warnings


def test_style_is_matched_canonically():
    out, warnings = _run([_concept(1, style="photoreal editorial")])
    assert _casts(out) == [True] and warnings == []


def test_prompt_without_a_human_subject_clears_the_cast():
    prompt = "A cinematic film still of Rocket Skates on a desert road at dusk."
    out, _ = _run([_concept(1, style="Cinematic film still", prompt=prompt)])
    assert _casts(out) == [False]
    assert "human subject" in out[0]["person_casting_reason"]


def test_the_reference_phrase_alone_is_not_a_human_subject():
    # "person" in the reference wording must not satisfy the cue by itself.
    prompt = (
        "A cinematic film still of the person in the person reference image "
        "beside Rocket Skates on a desert road."
    )
    out, _ = _run([_concept(1, style="Cinematic film still", prompt=prompt)])
    assert _casts(out) == [False]
    assert "human subject" in out[0]["person_casting_reason"]
    # With another human cue it stays cast.
    prompt = (
        "A cinematic film still of a skater, the person in the person reference "
        "image, on a desert road."
    )
    out, _ = _run([_concept(1, style="Cinematic film still", prompt=prompt)])
    assert _casts(out) == [True]


def test_human_cue_words_count():
    for prompt in (
        "A photoreal shot of a woman skating, the hero of the scene.",
        "Editorial portrait of an athlete mid-stride.",
        "A guitarist on stage under warm light.",
    ):
        out, _ = _run([_concept(1, style="Photoreal / editorial", prompt=prompt)])
        assert _casts(out) == [True], prompt
        # A cast prompt is pointed at the reference image.
        assert PERSON_HERO_PHRASE in out[0]["image_generation_prompt"].lower()


def test_cap_keeps_the_first_n_in_concept_order():
    concepts = [_concept(i) for i in range(1, 5)]
    out, warnings = _run(concepts, max_cast=2)
    assert _casts(out) == [True, True, False, False]
    assert len(warnings) == 2
    assert "at most 2" in out[2]["person_casting_reason"]


def test_cap_counts_only_valid_casts():
    concepts = [_concept(1, style="Meme aesthetic"), _concept(2), _concept(3)]
    out, _ = _run(concepts, max_cast=2)
    assert _casts(out) == [False, True, True]


def test_zero_cap_turns_casting_off():
    out, _ = _run([_concept(1)], max_cast=0)
    assert _casts(out) == [False]


def test_non_bool_flags_normalise_to_false_and_inputs_are_not_mutated():
    concepts = [_concept(1, cast="yes"), _concept(2)]
    snapshot = json.loads(json.dumps(concepts))
    out, _ = _run(concepts)
    assert _casts(out) == [False, True]
    assert concepts == snapshot


def test_callback_runs_the_casting_guard(monkeypatch):
    from creative_agent import callbacks

    concepts = [_concept(i) for i in range(1, 4)]
    state = {
        "brand": "ACME",
        "target_product": "Rocket Skates",
        "person_reference": {
            "uri": "gs://b/person-refs/a-1/me.jpg",
            "consent_id": "c1234567",
        },
        "final_visual_concepts": {"visual_concepts": concepts},
    }
    callbacks.ensure_trend_and_product_callback(SimpleNamespace(state=state))
    out = state["final_visual_concepts"]["visual_concepts"]
    assert _casts(out) == [True, True, False]


def test_callback_clears_casts_without_a_reference():
    from creative_agent import callbacks

    state = {
        "brand": "ACME",
        "target_product": "Rocket Skates",
        "final_visual_concepts": json.dumps({"visual_concepts": [_concept(1)]}),
    }
    callbacks.ensure_trend_and_product_callback(SimpleNamespace(state=state))
    out = json.loads(state["final_visual_concepts"])["visual_concepts"]
    assert _casts(out) == [False]
