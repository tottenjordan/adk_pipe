"""creative_eval: a cast concept's judge request carries the consented person's
reference photo (ground truth for their own appearance) after the rendered image."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from google.genai import errors as genai_errors

from creative_eval import prompts as eval_prompts
from creative_eval.config import EvalConfig
from creative_eval.dimensions import VISUAL_GATES
from creative_eval.evaluate import (
    evaluate_all_concurrently,
    evaluate_visual_concept,
    person_photo_uri,
)
from tests._eval_fakes import verdict_dicts

BUCKET = "tt-bucket"
PHOTO = f"gs://{BUCKET}/person-refs/me-abc/face.jpg"
RENDER = f"gs://{BUCKET}/out/hero.png"
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
}
CAST = {"gcs_uri": RENDER, "cast": True, "qa": None}
UNCAST = {"gcs_uri": RENDER, "cast": False, "qa": None}


@pytest.fixture(autouse=True)
def _bucket(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_STORAGE_BUCKET", BUCKET)


def _body() -> str:
    return json.dumps(
        {
            "ad_copy_id": 1,
            "concept_name": "Hero",
            "score": {
                "verdicts": verdict_dicts("visual", 9),
                "strengths": [],
                "improvements": [],
                "gates": [
                    {"gate": g, "passed": True, "note": "n"} for g in VISUAL_GATES
                ],
            },
        }
    )


def _client() -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=_body())
    return client


def _contents(client, call: int = -1) -> list:
    contents = client.models.generate_content.call_args_list[call].kwargs["contents"]
    return contents if isinstance(contents, list) else [contents]


def _uris(contents) -> list[str]:
    return [
        p.file_data.file_uri
        for p in contents
        if getattr(getattr(p, "file_data", None), "file_uri", None)
    ]


def _text(contents) -> str:
    return "\n".join(
        (p if isinstance(p, str) else getattr(p, "text", None) or "") for p in contents
    )


def _judge(image, person_reference, client=None):
    client = client or _client()
    result = evaluate_visual_concept(
        CONCEPT,
        CAMPAIGN,
        EvalConfig(),
        client=client,
        image=image,
        person_photo=person_photo_uri(person_reference),
    )
    return client, result


def test_person_photo_uri_shape_and_bucket(monkeypatch):
    assert person_photo_uri({"uri": PHOTO, "consent_id": "c1"}) == PHOTO
    for bad in (
        None,
        {},
        {"uri": ""},
        {"uri": f"gs://{BUCKET}/out/face.jpg"},
        {"uri": f"gs://{BUCKET}/person-refs/../x/face.jpg"},
        {"uri": "https://example.com/person-refs/me/face.jpg"},
        {"uri": "gs://other-bucket/person-refs/me-abc/face.jpg"},
    ):
        assert person_photo_uri(bad) == ""
    monkeypatch.delenv("GOOGLE_CLOUD_STORAGE_BUCKET")
    other = "gs://other-bucket/person-refs/me-abc/face.jpg"
    assert person_photo_uri({"uri": other}) == other


def test_person_photo_uri_agrees_with_the_engine_shape_check():
    from creative_agent.person_render import person_reference_uri

    for value in (
        {"uri": PHOTO},
        {"uri": f" {PHOTO} "},
        {"uri": f"gs://{BUCKET}/person-refs/a/b/c.jpg"},
        {"uri": f"gs://{BUCKET}/person-refs/me/"},
        {"uri": 5},
        "x",
    ):
        assert person_photo_uri(value) == person_reference_uri(value)


def test_cast_concept_gets_the_photo_after_the_render_with_ground_truth_text():
    client, result = _judge(CAST, {"uri": PHOTO, "consent_id": "c1"})
    contents = _contents(client)
    assert _uris(contents) == [RENDER, PHOTO]
    # Order: render, label, photo, prompt.
    assert getattr(contents[1], "text", "") == eval_prompts.PERSON_PHOTO_LABEL
    assert "Reference photo of the consented person cast as the hero:" in (
        eval_prompts.PERSON_PHOTO_LABEL
    )
    assert contents[2].file_data.mime_type == "image/jpeg"
    text = _text(contents)
    assert eval_prompts.VISUAL_PERSON_PHOTO_ATTACHED in text
    for word in ("tattoos", "piercings", "glasses", "hallucinations"):
        assert word in eval_prompts.VISUAL_PERSON_PHOTO_ATTACHED
    assert result.image_judged is True
    assert PHOTO not in json.dumps(result.model_dump())


def test_uncast_concept_never_gets_the_photo():
    client, _ = _judge(UNCAST, {"uri": PHOTO, "consent_id": "c1"})
    contents = _contents(client)
    assert _uris(contents) == [RENDER]
    assert eval_prompts.VISUAL_PERSON_PHOTO_ATTACHED not in _text(contents)


def test_missing_or_invalid_photo_judges_without_it():
    for ref in (None, {}, {"uri": "gs://other-bucket/person-refs/me/face.jpg"}):
        client, result = _judge(CAST, ref)
        contents = _contents(client)
        assert _uris(contents) == [RENDER]
        assert eval_prompts.VISUAL_PERSON_PHOTO_ATTACHED not in _text(contents)
        assert result.image_judged is True and result.score.gates_passed


def test_no_render_means_no_photo():
    client, result = _judge({"cast": True}, {"uri": PHOTO})
    assert _uris(_contents(client)) == []
    assert result.image_judged is False


def test_unreadable_photo_retries_with_the_render_only(caplog):
    client = _client()
    error = genai_errors.ClientError(
        404, {"error": {"message": f"file {PHOTO} not found", "status": "X"}}
    )
    client.models.generate_content.side_effect = [
        error,
        MagicMock(text=_body()),
    ]
    with caplog.at_level("DEBUG"):
        _, result = _judge(CAST, {"uri": PHOTO}, client=client)
    assert _uris(_contents(client, 0)) == [RENDER, PHOTO]
    assert _uris(_contents(client, 1)) == [RENDER]
    assert result.image_judged is True
    assert PHOTO not in caplog.text


def test_failed_judge_never_logs_the_photo_uri(caplog):
    client = _client()
    client.models.generate_content.side_effect = RuntimeError(f"boom {PHOTO}")
    with caplog.at_level("DEBUG"):
        _judge(CAST, {"uri": PHOTO}, client=client)
    assert PHOTO not in caplog.text


def test_evaluate_all_concurrently_threads_the_person_reference():
    client = _client()
    _, visuals = evaluate_all_concurrently(
        [],
        [CONCEPT],
        CAMPAIGN,
        EvalConfig(),
        client,
        generated_images={"Hero": CAST},
        person_reference={"uri": PHOTO, "consent_id": "c1"},
    )
    assert _uris(_contents(client)) == [RENDER, PHOTO]
    assert visuals[0].image_judged is True


def test_evaluate_all_creatives_reads_person_reference_from_state(monkeypatch):
    from creative_eval import agent as ev_agent

    seen = {}

    def fake(*args, **kwargs):
        seen.update(kwargs)
        return [], []

    monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", fake)

    class Ctx:
        state = {
            **CAMPAIGN,
            "target_search_trends": "roadrunner",
            "final_visual_concepts": {"visual_concepts": [CONCEPT]},
            "generated_images": {"Hero": CAST},
            "person_reference": {"uri": PHOTO, "consent_id": "c1"},
        }

    ev_agent.evaluate_all_creatives(Ctx())
    assert seen["person_reference"] == {"uri": PHOTO, "consent_id": "c1"}
    assert PHOTO not in json.dumps(Ctx.state["creative_evaluation_report"])
