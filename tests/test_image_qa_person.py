"""Image QA likeness for cast concepts (creative_agent/image_qa.py person fields
and rules) and the generate_image wiring (the person photo goes to QA, a
re-render keeps the person part)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from creative_agent import image_qa, image_tools
from creative_agent.image_qa import ImageQAResult
from tests._fakes import FakeToolContext, noop_async

PERSON_URI = "gs://b/person-refs/alice-0123456789/me.jpg"
_CAST = {
    "concept_name": "Hero",
    "image_generation_prompt": (
        "A candid 35mm film photo of a skater, the person in the person reference image "
        "skating past a lottery ball."
    ),
    "trend_motif": "a lottery ball",
    "visual_style": "Candid 35mm film photo",
    "casts_person_reference": True,
    "person_casting_reason": "Solo hero.",
}
_UNCAST = {**_CAST, "casts_person_reference": False}


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


def _rules(result, concept):
    return image_qa.qa_failed_rules(result, concept, target_product="skates")


def test_old_payloads_parse_with_person_defaults():
    payload = _result().model_dump()
    for key in ("person_cast", "person_likeness", "person_distorted"):
        payload.pop(key)
    parsed = ImageQAResult.model_validate_json(json.dumps(payload))
    assert parsed.person_cast is None and parsed.person_likeness is None
    assert parsed.person_distorted is False


def test_person_rules_fire_only_for_cast_concepts():
    lost = _result(person_cast=True, person_likeness=False)
    distorted = _result(person_cast=True, person_likeness=True, person_distorted=True)
    assert _rules(lost, _CAST) == ["person likeness lost"]
    assert _rules(distorted, _CAST) == ["person distorted"]
    assert _rules(lost, _UNCAST) == [] and _rules(distorted, _UNCAST) == []


def test_unknown_likeness_is_not_a_failure():
    assert _rules(_result(person_likeness=None), _CAST) == []


def test_person_rules_are_not_critical():
    assert "person likeness lost" not in image_qa.CRITICAL_RULES
    assert "person distorted" not in image_qa.CRITICAL_RULES


def test_correction_restates_the_likeness_instruction():
    result = _result(
        person_cast=True, person_likeness=False, issues=["hero has a different face"]
    )
    text = image_qa.correction_text(result, _CAST, target_product="skates")
    assert "hero has a different face" in text
    assert "exactly the same person as the person reference image" in text
    assert text.endswith(image_qa.NO_NEW_TEXT)


class _QAClient:
    def __init__(self):
        self.calls = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=_result().model_dump_json(), parsed=None)


def _inspect(person_image=None):
    client = _QAClient()
    kwargs = {"person_image": person_image} if person_image else {}
    image_qa.inspect_image(
        b"render",
        "image/png",
        _CAST,
        brand="ACME",
        target_product="skates",
        client=client,
        model="m",
        **kwargs,
    )
    contents = client.calls[0]["contents"]
    text = " ".join(p.text for p in contents if getattr(p, "text", None))
    images = [p.inline_data.data for p in contents if getattr(p, "inline_data", None)]
    return text, images


def test_instruction_compares_with_the_person_image_only_when_given():
    text, images = _inspect((b"face", "image/jpeg"))
    assert images == [b"render", b"face"]
    assert "Is the hero clearly the same person as the reference image?" in text
    assert "person_distorted" in text
    text, images = _inspect()
    assert images == [b"render"]
    assert "same person as the reference image" not in text
    assert "person_cast and person_likeness null" in text


# --- generate_image wiring ----------------------------------------------------------


def _img(data):
    inline = SimpleNamespace(data=data, mime_type="image/png")
    content = SimpleNamespace(
        parts=[SimpleNamespace(inline_data=inline, thought=False)]
    )
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=content, finish_reason=None)],
        prompt_feedback=None,
    )


def test_cast_concept_qa_gets_the_photo_and_rerender_keeps_the_person(monkeypatch):
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        return _img(f"img{len(calls)}".encode())

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    monkeypatch.setattr(image_tools, "_get_genai_client", lambda: client)
    monkeypatch.setattr(image_tools.asyncio, "sleep", noop_async)
    monkeypatch.setattr(image_tools, "_save_to_gcs", lambda **k: "gs://b/out.png")
    monkeypatch.setattr(image_tools, "_download_blob", lambda b, o: b"face")
    monkeypatch.setattr(image_tools.config, "GCS_BUCKET_NAME", "b")
    monkeypatch.setattr(image_tools.config, "image_qa_enabled", True)
    monkeypatch.setattr(image_tools.config, "image_qa_max_rerenders", 1)
    monkeypatch.setattr(image_tools.config, "image_qa_max_rerenders_per_run", 2)
    monkeypatch.setattr(image_qa, "_get_qa_client", lambda: "qa-client")
    verdicts = [
        _result(person_cast=True, person_likeness=False),
        _result(person_cast=True, person_likeness=True),
    ]
    seen = []

    def fake_inspect(image_bytes, mime, concept, **kwargs):
        seen.append((image_bytes, kwargs.get("person_image")))
        return verdicts.pop(0)

    monkeypatch.setattr(image_qa, "inspect_image", fake_inspect)
    ctx = FakeToolContext(
        {
            "gcs_folder": "f",
            "agent_output_dir": "d",
            "target_product": "skates",
            "person_reference": {"uri": PERSON_URI, "consent_id": "consent-1234"},
            "final_visual_concepts": {"visual_concepts": [dict(_CAST)]},
        }
    )
    asyncio.run(image_tools.generate_image(ctx))

    assert len(calls) == 2
    for call in calls:  # first render and the QA re-render both cast
        assert "(person)" in call["contents"][0]
        assert call["config"].image_config.person_generation == "ALLOW_ADULT"
    assert "same person as the person reference image" in calls[1]["contents"][0]
    assert seen == [
        (b"img1", (b"face", "image/jpeg")),
        (b"img2", (b"face", "image/jpeg")),
    ]
    record = ctx.state["generated_images"]["Hero"]
    assert record["cast"] is True and record["attempts"] == 2
    assert record["qa"]["passed"] is True
