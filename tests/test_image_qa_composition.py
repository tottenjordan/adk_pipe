"""Image QA composition check for personalised variant edits
(creative_agent/image_qa.py ``original_image``, ``composition_changed``)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from creative_agent import image_qa, image_tools
from creative_agent.image_qa import ImageQAResult

_CONCEPT = {
    "concept_name": "Hero",
    "image_generation_prompt": "A candid photo of a referee holding a flag.",
    "trend_motif": "a penalty flag",
    "visual_style": "Candid 35mm film photo",
    "casts_person_reference": True,
}


def _result(**overrides) -> ImageQAResult:
    base = {
        "product_visible": True,
        "motif_visible": True,
        "text_expected": False,
        "gibberish_text": False,
        "unrequested_logos": False,
        "artifacts": False,
        "unsafe": False,
    }
    return ImageQAResult(**(base | overrides))


def _rules(result):
    return image_qa.qa_failed_rules(result, _CONCEPT, target_product="soda")


def test_old_payloads_parse_with_composition_defaults():
    payload = _result().model_dump()
    for key in image_qa.COMPOSITION_FIELDS:
        payload.pop(key)
    parsed = ImageQAResult.model_validate_json(json.dumps(payload))
    assert parsed.composition_changed is False
    assert parsed.composition_differences == []


def test_composition_rule_is_not_critical():
    changed = _result(composition_changed=True, composition_differences=["x"])
    assert _rules(changed) == ["composition changed"]
    assert _rules(_result()) == []
    assert "composition changed" not in image_qa.CRITICAL_RULES


def test_correction_restates_keep_everything_and_lists_the_differences():
    result = _result(
        composition_changed=True,
        composition_differences=[
            "6 flags instead of 2",
            'the "DEFENSIVE SHIELD" label is {missing}',
        ],
        issues=["busier composition"],
    )
    text = image_qa.correction_text(result, _CONCEPT, target_product="soda")
    assert image_qa.COMPOSITION_PHRASE in text
    assert "6 flags instead of 2" in text
    assert "DEFENSIVE SHIELD label is missing" in text
    assert "{" not in text and '"' not in text
    assert text.endswith(image_qa.NO_NEW_TEXT)


def test_correction_caps_the_differences():
    many = [f"difference number {i} " + "x" * 300 for i in range(10)]
    result = _result(composition_changed=True, composition_differences=many)
    text = image_qa.correction_text(result, _CONCEPT, target_product="soda")
    assert text.count("difference number") == image_qa.MAX_COMPOSITION_DIFFERENCES
    assert "x" * (image_qa.ISSUE_CHAR_CAP + 1) not in text


class _QAClient:
    def __init__(self, verdict: ImageQAResult):
        self.calls = []
        self.models = self
        self.verdict = verdict

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.verdict.model_dump_json(), parsed=None)


def _inspect(verdict, original_image=None):
    client = _QAClient(verdict)
    kwargs = {"original_image": original_image} if original_image else {}
    result = image_qa.inspect_image(
        b"render",
        "image/png",
        _CONCEPT,
        brand="ACME",
        target_product="soda",
        client=client,
        model="m",
        person_image=(b"face", "image/jpeg"),
        **kwargs,
    )
    contents = client.calls[0]["contents"]
    text = " ".join(p.text for p in contents if getattr(p, "text", None))
    images = [p.inline_data.data for p in contents if getattr(p, "inline_data", None)]
    return result, text, images


def test_instruction_and_original_only_when_given():
    changed = _result(composition_changed=True, composition_differences=["moved"])
    result, text, images = _inspect(changed, (b"orig", "image/png"))
    assert images == [b"render", b"face", b"orig"]
    assert image_qa.ORIGINAL_LABEL in text
    assert "Set composition_changed and list concrete differences" in text
    assert result.composition_changed is True
    assert _rules(result) == ["composition changed"]

    result, text, images = _inspect(changed)
    assert images == [b"render", b"face"]
    assert "composition_changed" not in text and image_qa.ORIGINAL_LABEL not in text
    # Without an original nothing was compared: the rule never fires.
    assert result.composition_changed is False
    assert result.composition_differences == []
    assert _rules(result) == []


def test_stored_record_has_composition_fields_only_for_edits():
    result = _result()
    base = image_tools._qa_record(result, _CONCEPT, "soda")
    assert not set(image_qa.COMPOSITION_FIELDS) & set(base)
    edit = image_tools._qa_record(result, _CONCEPT, "soda", compared_to_original=True)
    assert edit["composition_changed"] is False
    assert edit["composition_differences"] == []
