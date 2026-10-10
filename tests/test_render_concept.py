"""creative_agent/render_concept.py: single-concept renders without a ToolContext
(the batch parity with the pre-refactor generate_image is in
tests/test_render_parity.py)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from google.genai import types

from creative_agent import image_qa, image_tools
from creative_agent.image_qa import ImageQAResult
from creative_agent.person_render import PERSON_ROLE_INSTRUCTION
from creative_agent.render_concept import (
    PHOTO_UNAVAILABLE,
    RenderReferences,
    render_concept,
    render_contents,
)
from tests._fakes import noop_async

PHOTO = types.Part.from_bytes(data=b"face", mime_type="image/jpeg")
PRODUCT = types.Part.from_bytes(data=b"prod", mime_type="image/png")
CONCEPT = {
    "concept_name": "Hero",
    "image_generation_prompt": "a candid photo of a skater with a guitar",
    "trend_motif": "a lottery ball",
    "visual_style": "Candid 35mm film photo",
}


def _verdict(**overrides) -> ImageQAResult:
    base = dict(
        product_visible=True,
        motif_visible=True,
        brand_cue_visible=True,
        text_expected=False,
        text_exact=True,
        text_legible=True,
        gibberish_text=False,
        unrequested_logos=False,
        artifacts=False,
        unsafe=False,
        issues=[],
    )
    base.update(overrides)
    return ImageQAResult(**base)


def _response(data: bytes | None, finish=types.FinishReason.STOP):
    parts = []
    if data is not None:
        inline = SimpleNamespace(data=data, mime_type="image/png")
        parts.append(SimpleNamespace(inline_data=inline, thought=False))
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=parts), finish_reason=finish
    )
    return SimpleNamespace(candidates=[candidate], prompt_feedback=None)


def _patch(monkeypatch, respond, verdicts=()):
    calls: list[dict] = []
    qa_calls: list[dict] = []
    queue = list(verdicts)

    def generate_content(**kwargs):
        calls.append(kwargs)
        return respond(kwargs, len(calls))

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    monkeypatch.setattr(image_tools, "_get_genai_client", lambda: client)
    monkeypatch.setattr(image_tools.asyncio, "sleep", noop_async)
    monkeypatch.setattr(image_tools.config, "image_qa_max_rerenders", 1)
    monkeypatch.setattr(image_qa, "_get_qa_client", lambda: "qa")

    def fake_inspect(image_bytes, mime, concept, **kwargs):
        qa_calls.append({"image": image_bytes, "concept": concept, **kwargs})
        return queue.pop(0) if queue else _verdict()

    monkeypatch.setattr(image_qa, "inspect_image", fake_inspect)
    return calls, qa_calls


def test_render_contents_orders_references_then_person():
    refs = RenderReferences(parts=(("product", PRODUCT),), missing_roles=("logo",))
    contents = render_contents(
        "PROMPT", refs, brand="PRS", strictness=("unwanted_logo",), person_part=PHOTO
    )
    text, product, person = contents
    assert text.startswith("PROMPT\n\nNo logos, brand marks or trademarks except")
    assert "Reference image 1 (product)" in text
    assert f"Reference image 2 (person): {PERSON_ROLE_INSTRUCTION}" in text
    assert "The logo reference image is unavailable" in text
    assert (product, person) == (PRODUCT, PHOTO)
    # No parts → the bare prompt (text-only render).
    assert render_contents("PROMPT", RenderReferences()) == "PROMPT"


def test_plain_render_with_qa_rerender(monkeypatch):
    calls, qa_calls = _patch(
        monkeypatch,
        lambda _k, n: _response(f"img{n}".encode()),
        verdicts=[_verdict(motif_visible=False), _verdict()],
    )
    result = asyncio.run(
        render_concept(
            CONCEPT,
            aspect_ratio="4:5",
            references=RenderReferences(parts=(("product", PRODUCT),)),
            brand="PRS",
            target_product="guitar",
        )
    )
    assert result.rendered == (b"img2", "image/png")
    assert result.attempts == 2 and result.qa["passed"] is True
    assert result.cast is False and result.rejected_reason is None
    assert len(calls) == 2 and len(qa_calls) == 2
    assert calls[0]["config"].image_config.aspect_ratio == "4:5"
    assert calls[0]["config"].image_config.person_generation is None
    assert "person_image" not in qa_calls[0]


def test_cast_render_attaches_photo_and_checks_likeness(monkeypatch):
    calls, qa_calls = _patch(monkeypatch, lambda _k, n: _response(b"cast"))
    concept = {**CONCEPT, "casts_person_reference": True}
    result = asyncio.run(
        render_concept(
            concept,
            aspect_ratio="9:16",
            person=PHOTO,
            fallback_without_person=False,
        )
    )
    assert result.cast is True and result.rendered == (b"cast", "image/png")
    assert calls[0]["config"].image_config.person_generation == "ALLOW_ADULT"
    assert calls[0]["contents"][-1] is PHOTO
    assert qa_calls[0]["person_image"] == (b"face", "image/jpeg")


def test_blocked_person_without_fallback_is_rejected(monkeypatch):
    calls, qa_calls = _patch(
        monkeypatch,
        lambda _k, _n: _response(None, finish=types.FinishReason.IMAGE_SAFETY),
    )
    concept = {**CONCEPT, "casts_person_reference": True}
    result = asyncio.run(
        render_concept(
            concept, aspect_ratio="1:1", person=PHOTO, fallback_without_person=False
        )
    )
    assert result.rendered is None
    assert result.rejected_reason == "image_safety"
    assert len(calls) == 1 and qa_calls == []  # no person-less fallback render


def test_missing_photo_without_fallback_renders_nothing(monkeypatch):
    calls, _ = _patch(monkeypatch, lambda _k, n: _response(b"x"))
    concept = {**CONCEPT, "casts_person_reference": True}
    result = asyncio.run(
        render_concept(concept, aspect_ratio="1:1", fallback_without_person=False)
    )
    assert result.rendered is None and result.rejected_reason == PHOTO_UNAVAILABLE
    assert calls == []


def test_blocked_person_with_fallback_renders_generic_hero(monkeypatch):
    def respond(kwargs, _n):
        if kwargs["config"].image_config.person_generation:
            return _response(None, finish=types.FinishReason.IMAGE_SAFETY)
        return _response(b"generic")

    calls, qa_calls = _patch(monkeypatch, respond)
    concept = {
        **CONCEPT,
        "image_generation_prompt": CONCEPT["image_generation_prompt"]
        + " The hero is the person in the person reference image.",
        "casts_person_reference": True,
    }
    result = asyncio.run(render_concept(concept, aspect_ratio="1:1", person=PHOTO))
    assert result.rendered == (b"generic", "image/png")
    assert result.cast is False and result.rejected_reason == "image_safety"
    assert result.concept["casts_person_reference"] is False
    assert "person reference image" not in result.concept["image_generation_prompt"]
    assert len(calls) == 2 and "person_image" not in qa_calls[0]


def test_qa_off_and_qa_error(monkeypatch):
    calls, qa_calls = _patch(monkeypatch, lambda _k, n: _response(b"img"))
    off = asyncio.run(render_concept(CONCEPT, aspect_ratio="1:1", qa=False))
    assert off.qa is None and off.qa_unavailable is False and qa_calls == []

    def broken(*_a, **_k):
        raise RuntimeError("qa down")

    monkeypatch.setattr(image_qa, "inspect_image", broken)
    errored = asyncio.run(render_concept(CONCEPT, aspect_ratio="1:1"))
    assert errored.rendered == (b"img", "image/png")
    assert errored.qa is None and errored.qa_unavailable is True


# --- generate_image batch behaviour on failures ---------------------------------------


def _batch(monkeypatch, respond, verdicts=()):
    from tests._fakes import FakeToolContext

    calls, qa_calls = _patch(monkeypatch, respond, verdicts)
    uploads: list[str] = []

    def fake_save(*, tool_context, image_bytes, filename):
        uploads.append(filename)
        return f"gs://b/{filename}"

    monkeypatch.setattr(image_tools, "_save_to_gcs", fake_save)
    monkeypatch.setattr(image_tools.config, "image_qa_enabled", True)
    concepts = [
        {
            **CONCEPT,
            "concept_name": f"C{i}",
            "image_generation_prompt": f"C{i}: a skater",
        }
        for i in (1, 2, 3)
    ]
    ctx = FakeToolContext(
        {
            "gcs_folder": "f",
            "agent_output_dir": "d",
            "final_visual_concepts": {"visual_concepts": concepts},
        }
    )
    return ctx, calls, qa_calls, uploads


def _name(kwargs) -> str:
    contents = kwargs["contents"]
    return (contents if isinstance(contents, str) else contents[0]).split(":", 1)[0]


def test_batch_render_error_aborts_before_any_upload(monkeypatch):
    """Deliberate change from the pre-refactor loop: a first-render exception
    aborts the whole batch before ANY upload (earlier, finished concepts are not
    uploaded either) and later concepts never render; the error propagates so the
    node's RetryConfig can retry."""

    def respond(kwargs, n):
        if _name(kwargs) == "C2":
            raise ValueError("bad request")
        return _response(f"img{n}".encode())

    ctx, calls, _qa, uploads = _batch(monkeypatch, respond)
    try:
        asyncio.run(image_tools.generate_image(ctx))
    except ValueError:
        pass
    else:
        raise AssertionError("expected the render error to propagate")
    assert [_name(c) for c in calls] == ["C1", "C2"]  # C3 never rendered
    assert uploads == []
    assert "generated_images" not in ctx.state
    assert not ctx.state.get("_images_generated")


def test_batch_render_without_an_image_skips_only_that_concept(monkeypatch):
    def respond(kwargs, n):
        return _response(None if _name(kwargs) == "C2" else f"img{n}".encode())

    ctx, _calls, qa_calls, uploads = _batch(monkeypatch, respond)
    asyncio.run(image_tools.generate_image(ctx))
    assert uploads == ["C1.png", "C3.png"]
    assert list(ctx.state["generated_images"]) == ["C1", "C3"]
    assert sorted(q["concept"]["concept_name"] for q in qa_calls) == ["C1", "C3"]
    assert "image_qa__unavailable" not in ctx.state
    assert "image_qa__issues" not in ctx.state


def test_batch_qa_error_keeps_the_render_and_lists_it_unavailable(monkeypatch):
    def respond(kwargs, n):
        return _response(f"img{n}".encode())

    ctx, _calls, _qa, uploads = _batch(monkeypatch, respond)

    def flaky(image_bytes, mime, concept, **kwargs):
        if concept["concept_name"] == "C2":
            raise RuntimeError("qa down")
        return _verdict()

    monkeypatch.setattr(image_qa, "inspect_image", flaky)
    asyncio.run(image_tools.generate_image(ctx))
    assert uploads == ["C1.png", "C2.png", "C3.png"]
    images = ctx.state["generated_images"]
    assert images["C2"]["qa"] is None and images["C2"]["attempts"] == 1
    assert images["C1"]["qa"]["passed"] is True
    assert ctx.state["image_qa__unavailable"] == ["C2"]
    assert "image_qa__issues" not in ctx.state
