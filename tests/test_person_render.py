"""Person casting at render time: creative_agent/person_render.py helpers and the
generate_image integration (person part + ALLOW_ADULT for cast concepts only,
typed safety fallback, URI restriction)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors
from google.genai import types

from creative_agent import image_tools
from creative_agent.person_render import (
    PERSON_ROLE_INSTRUCTION,
    person_block_reason,
    person_consent_id,
    person_image_config,
    person_reference_uri,
)
from tests._fakes import FakeToolContext, noop_async

PERSON_URI = "gs://b/person-refs/alice-0123456789/me.jpg"
CAST_PROMPT = (
    "A candid 35mm film photo of the person in the person reference image "
    "laughing on a rooftop with Rocket Skates."
)


# --- pure helpers -------------------------------------------------------------------


def test_person_reference_uri_shape():
    assert person_reference_uri({"uri": PERSON_URI}) == PERSON_URI
    for bad in (
        {"uri": "gs://b/products/me.jpg"},
        {"uri": "gs://b/person-refs/me.jpg"},  # no owner folder
        {"uri": "gs://b/person-refs/a/../b/me.jpg"},
        {"uri": "https://b/person-refs/a/me.jpg"},
        {"uri": 3},
        {},
        None,
        PERSON_URI,
    ):
        assert person_reference_uri(bad) == "", bad


def test_person_consent_id():
    assert person_consent_id({"consent_id": " c1 "}) == "c1"
    assert person_consent_id({}) == "" and person_consent_id(None) == ""


def _response(*, image=True, finish=None, block=None):
    inline = SimpleNamespace(data=b"img", mime_type="image/png")
    parts = [SimpleNamespace(inline_data=inline, thought=False)]
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=parts if image else []), finish_reason=finish
    )
    feedback = SimpleNamespace(block_reason=block) if block else None
    return SimpleNamespace(candidates=[candidate], prompt_feedback=feedback)


def test_person_block_reason():
    assert person_block_reason(_response()) is None
    assert (
        person_block_reason(_response(image=False, block=types.BlockedReason.SAFETY))
        == "prompt_blocked:safety"
    )
    for finish in ("IMAGE_SAFETY", "PROHIBITED_CONTENT", "SAFETY"):
        got = person_block_reason(
            _response(image=False, finish=getattr(types.FinishReason, finish))
        )
        assert got == finish.lower()
    assert person_block_reason(_response(image=False)) == "no_image"
    assert person_block_reason(SimpleNamespace(candidates=None)) == "no_image"
    # STOP with an image is fine.
    assert person_block_reason(_response(finish=types.FinishReason.STOP)) is None


def test_person_image_config_adds_allow_adult():
    base = types.ImageConfig(aspect_ratio="4:5", image_size="2K")
    got = person_image_config(base)
    assert got.person_generation == "ALLOW_ADULT"
    assert got.aspect_ratio == "4:5" and got.image_size == "2K"
    assert base.person_generation is None


# --- generate_image integration -----------------------------------------------------


class _Models:
    """Records each generate_content call; ``respond(kwargs)`` → response."""

    def __init__(self, respond):
        self.calls = []
        self._respond = respond

    def generate_content(self, *a, **k):
        self.calls.append(k)
        return self._respond(k)


def _ok(_k):
    return _response()


def _patch(monkeypatch, respond=_ok, *, photo=b"\xff\xd8person"):
    models = _Models(respond)
    client = SimpleNamespace(models=models)
    monkeypatch.setattr(image_tools, "_get_genai_client", lambda: client)
    monkeypatch.setattr(image_tools.asyncio, "sleep", noop_async)
    monkeypatch.setattr(image_tools, "_save_to_gcs", lambda *a, **k: "gs://b/out.png")
    downloads = []

    def fake_download(bucket, obj):
        downloads.append((bucket, obj))
        if photo is None:
            raise RuntimeError("gone")
        return photo

    monkeypatch.setattr(image_tools, "_download_blob", fake_download)
    monkeypatch.setattr(image_tools.config, "GCS_BUCKET_NAME", "b")
    return models, downloads


def _concept(name, *, cast, style="Candid 35mm film photo", prompt=CAST_PROMPT):
    return {
        "concept_name": name,
        "visual_style": style,
        "image_generation_prompt": prompt,
        "casts_person_reference": cast,
        "person_casting_reason": "Solo hero." if cast else "",
    }


def _ctx(concepts, *, person=None):
    state = {
        "gcs_folder": "f",
        "agent_output_dir": "d",
        "final_visual_concepts": {"visual_concepts": concepts},
    }
    if person is not None:
        state["person_reference"] = person
    return FakeToolContext(state)


_PERSON = {"uri": PERSON_URI, "consent_id": "consent-1234"}


def _is_person_call(call) -> bool:
    contents = call["contents"]
    return isinstance(contents, list) and "(person)" in contents[0]


@pytest.mark.usefixtures("image_qa_off")
def test_cast_concept_gets_person_part_and_allow_adult(monkeypatch):
    models, downloads = _patch(monkeypatch)
    ctx = _ctx(
        [
            _concept("cast", cast=True),
            _concept("plain", cast=False, prompt="A flat cartoon skate."),
        ],
        person=_PERSON,
    )
    asyncio.run(image_tools.generate_image(ctx))

    assert downloads == [("b", "person-refs/alice-0123456789/me.jpg")]  # once
    cast_call, plain_call = models.calls
    contents = cast_call["contents"]
    assert isinstance(contents, list) and len(contents) == 2
    assert f"Reference image 1 (person): {PERSON_ROLE_INSTRUCTION}" in contents[0]
    assert contents[1].inline_data.data == b"\xff\xd8person"
    assert cast_call["config"].image_config.person_generation == "ALLOW_ADULT"
    assert plain_call["contents"] == "A flat cartoon skate."
    assert plain_call["config"].image_config.person_generation is None

    images = ctx.state["generated_images"]
    assert images["cast"]["cast"] is True
    assert images["cast"]["consent_id"] == "consent-1234"
    assert images["plain"]["cast"] is False and "consent_id" not in images["plain"]
    assert "person_reference_rejected" not in ctx.state


@pytest.mark.usefixtures("image_qa_off")
def test_person_part_follows_other_references(monkeypatch):
    models, _ = _patch(monkeypatch)
    ctx = _ctx([_concept("cast", cast=True)], person=_PERSON)
    ctx.state["reference_images"] = [{"uri": "gs://b/p.png", "role": "product"}]
    asyncio.run(image_tools.generate_image(ctx))
    contents = models.calls[0]["contents"]
    assert len(contents) == 3
    assert "Reference image 1 (product)" in contents[0]
    assert "Reference image 2 (person)" in contents[0]


@pytest.mark.usefixtures("image_qa_off")
def test_blocked_person_render_falls_back_without_the_person(monkeypatch):
    def respond(k):
        if k["config"].image_config.person_generation:
            return _response(image=False, finish=types.FinishReason.IMAGE_SAFETY)
        return _response()

    models, _ = _patch(monkeypatch, respond)
    ctx = _ctx([_concept("cast", cast=True)], person=_PERSON)
    result = asyncio.run(image_tools.generate_image(ctx))

    assert result["status"] == "success"
    assert len(models.calls) == 2
    fallback = models.calls[1]
    assert not _is_person_call(fallback)
    assert fallback["config"].image_config.person_generation is None
    assert "person reference image" not in str(fallback["contents"])
    assert ctx.state["person_reference_rejected"] == {"cast": "image_safety"}
    assert ctx.state["generated_images"]["cast"]["cast"] is False
    assert ctx.state["person_reference__issues"] == [
        "cast: person photo rejected by the safety filter; rendered without the person"
    ]


@pytest.mark.usefixtures("image_qa_off")
def test_client_error_on_person_render_falls_back(monkeypatch):
    def respond(k):
        if k["config"].image_config.person_generation:
            raise genai_errors.ClientError(400, {"error": {"message": "nope"}})
        return _response()

    models, _ = _patch(monkeypatch, respond)
    ctx = _ctx([_concept("cast", cast=True)], person=_PERSON)
    asyncio.run(image_tools.generate_image(ctx))
    assert len(models.calls) == 2
    assert ctx.state["person_reference_rejected"] == {"cast": "request_rejected:400"}
    assert ctx.state["generated_images"]["cast"]["cast"] is False


@pytest.mark.usefixtures("image_qa_off")
def test_uri_outside_person_refs_is_ignored_and_never_logged(monkeypatch, caplog):
    models, downloads = _patch(monkeypatch)
    bad = {"uri": "gs://b/products/secret-face.jpg", "consent_id": "consent-1234"}
    ctx = _ctx([_concept("cast", cast=True)], person=bad)
    with caplog.at_level(logging.INFO):
        asyncio.run(image_tools.generate_image(ctx))
    assert downloads == []
    assert not _is_person_call(models.calls[0])
    assert "secret-face" not in caplog.text
    assert "person reference" in caplog.text.lower()
    assert ctx.state["generated_images"]["cast"].get("cast") is not True


@pytest.mark.usefixtures("image_qa_off")
def test_photo_fetch_failure_renders_without_the_person(monkeypatch, caplog):
    models, _ = _patch(monkeypatch, photo=None)
    ctx = _ctx([_concept("cast", cast=True)], person=_PERSON)
    with caplog.at_level(logging.INFO):
        asyncio.run(image_tools.generate_image(ctx))
    assert len(models.calls) == 1 and not _is_person_call(models.calls[0])
    assert ctx.state["person_reference_rejected"] == {"cast": "photo_unavailable"}
    assert ctx.state["generated_images"]["cast"]["cast"] is False
    assert "alice-0123456789" not in caplog.text


@pytest.mark.usefixtures("image_qa_off")
def test_render_reapplies_the_casting_guard(monkeypatch):
    models, downloads = _patch(monkeypatch)
    ctx = _ctx([_concept("meme", cast=True, style="Meme aesthetic")], person=_PERSON)
    asyncio.run(image_tools.generate_image(ctx))
    assert downloads == []
    assert not _is_person_call(models.calls[0])
    assert ctx.state["generated_images"]["meme"]["cast"] is False


@pytest.mark.usefixtures("image_qa_off")
def test_run_without_person_reference_records_no_cast_key(monkeypatch):
    models, downloads = _patch(monkeypatch)
    ctx = _ctx([_concept("plain", cast=False, prompt="p")])
    asyncio.run(image_tools.generate_image(ctx))
    assert downloads == []
    assert "cast" not in ctx.state["generated_images"]["plain"]
    assert models.calls[0]["config"].image_config.person_generation is None
