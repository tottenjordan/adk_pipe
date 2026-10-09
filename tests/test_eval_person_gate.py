"""creative_eval: the person_depicted_respectfully visual gate (decided in code
for uncast concepts) and the JUDGE_VERSION bump that came with it."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from creative_eval import JUDGE_VERSION
from creative_eval import prompts as eval_prompts
from creative_eval.config import EvalConfig
from creative_eval.dimensions import (
    ADVISORY_GATES,
    BRIEF_GATES,
    GATE_LABELS,
    PERSON_GATES,
    VISUAL_GATES,
)
from creative_eval.evaluate import (
    NO_PERSON_CAST_NOTE,
    evaluate_visual_concept,
    normalize_gates,
)
from creative_eval.schemas import GateResult

GATE = "person_depicted_respectfully"
CAMPAIGN = {
    "brand": "ACME",
    "target_product": "Rocket Skates",
    "target_audience": "runners",
    "key_selling_points": "fast",
    "target_search_trend": "roadrunner",
}
CONCEPT = {
    "ad_copy_id": 1,
    "concept_name": "Hero",
    "visual_style": "Candid 35mm film photo",
    "trend": "roadrunner",
    "trend_reference": "r",
    "markets_product": "m",
    "audience_appeal": "a",
    "selection_rationale": "s",
    "headline": "Beep",
    "social_caption": "Zoom",
    "call_to_action": "Shop now",
    "concept_summary": "sum",
    "image_generation_prompt": "the person in the person reference image skating",
    "casts_person_reference": True,
}


def test_gate_is_a_visual_gate_with_a_label_and_a_version_bump():
    assert VISUAL_GATES[-1] == GATE
    assert PERSON_GATES == frozenset({GATE})
    assert GATE not in ADVISORY_GATES and GATE not in BRIEF_GATES
    assert GATE_LABELS[GATE] == "Person depicted respectfully"
    assert JUDGE_VERSION == "2026-10-10"


def test_prompt_names_the_gate_and_the_new_count():
    text = eval_prompts.VISUAL_CONCEPT_EVAL_USER
    assert f"**{GATE}**" in text
    assert f"Return all {len(VISUAL_GATES)} gates" in text
    assert "Person Cast: {person_cast}" in text
    assert "no mockery, sexualisation, injury, or demeaning situations" in text


def _raw(passed: bool, note="looks fine"):
    return [GateResult(gate=g, passed=passed, note=note) for g in VISUAL_GATES]


def test_uncast_concept_passes_with_note_whatever_the_judge_says():
    gates = normalize_gates(_raw(False), VISUAL_GATES, brief_used=True)
    gate = next(g for g in gates if g.gate == GATE)
    assert gate.passed is True and gate.note == NO_PERSON_CAST_NOTE


def test_cast_concept_keeps_the_judges_verdict():
    gates = normalize_gates(
        _raw(False, "mocked"), VISUAL_GATES, brief_used=True, person_cast=True
    )
    gate = next(g for g in gates if g.gate == GATE)
    assert gate.passed is False and gate.note == "mocked"


def _judge(gate_passed: bool) -> MagicMock:
    body = {
        "ad_copy_id": 1,
        "concept_name": "Hero",
        "score": {
            "verdicts": [
                {"dimension": "d", "score": 9, "verdict": "pass", "rationale": "r"}
            ],
            "strengths": [],
            "improvements": [],
            "gates": [
                {"gate": g, "passed": g != GATE or gate_passed, "note": "n"}
                for g in VISUAL_GATES
            ],
        },
    }
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=json.dumps(body))
    return client


def _prompt(client) -> str:
    contents = client.models.generate_content.call_args.kwargs["contents"]
    if isinstance(contents, str):
        return contents
    return "\n".join(getattr(p, "text", None) or "" for p in contents)


def test_cast_read_from_generated_images_record():
    client = _judge(False)
    image = {"gcs_uri": "gs://b/hero.png", "cast": True, "qa": None}
    result = evaluate_visual_concept(
        CONCEPT, CAMPAIGN, EvalConfig(), client=client, image=image
    )
    assert "Person Cast: yes" in _prompt(client)
    assert not result.score.gates_passed and not result.score.passed


def test_concept_flag_alone_is_not_a_cast():
    # A blocked person render falls back without the person: the record says
    # cast False even though the concept asked to cast.
    for image in ({"gcs_uri": "gs://b/hero.png", "cast": False}, None):
        client = _judge(False)
        result = evaluate_visual_concept(
            CONCEPT, CAMPAIGN, EvalConfig(), client=client, image=image
        )
        assert "Person Cast: no" in _prompt(client)
        gate = next(g for g in result.score.gates if g.gate == GATE)
        assert gate.passed and gate.note == NO_PERSON_CAST_NOTE
        assert result.score.gates_passed


def test_unversioned_report_inference_is_unchanged():
    from runserver.ratings import judge_fields

    gates = [{"gate": g, "passed": True} for g in VISUAL_GATES]
    report = {"visual_concept_evaluations": [{"score": {"gates": gates}}]}
    # Unversioned reports predate stamping, so they still infer 2026-10-08.
    info = {"kind": "visual", "name": "x", "original_id": ""}
    assert judge_fields(report, info)["judge_version"] == "2026-10-08"
    stamped = {**report, "judge_version": JUDGE_VERSION}
    assert judge_fields(stamped, info)["judge_version"] == "2026-10-10"
