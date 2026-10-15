"""generate_image robustness: artifact-key collisions (CV-3), the render
fingerprint that re-renders changed concepts (CV-5) and the timely cap re-applied
at render time (IC-2)."""

from __future__ import annotations

import asyncio

import pytest

from creative_agent import image_tools
from tests._fakes import FakeToolContext


def _concept(name: str, prompt: str | None = None, **extra) -> dict:
    return {
        "concept_name": name,
        "visual_style": "Photoreal / editorial",
        "image_generation_prompt": prompt or f"PROMPT {name}: a guitar on a stage.",
        **extra,
    }


class _Run:
    def __init__(self, monkeypatch, concepts, **state):
        self.renders: list[str] = []
        self.uploads: dict[str, bytes] = {}

        async def render(contents, aspect_ratio, image_config=None):
            text = contents if isinstance(contents, str) else contents[0]
            self.renders.append(text)
            return f"img{len(self.renders)}".encode(), "image/png"

        def save(*, tool_context, image_bytes, filename, metadata=None):
            self.uploads[filename] = image_bytes
            return f"gs://b/f/d/{filename}"

        monkeypatch.setattr(image_tools, "_render_image", render)
        monkeypatch.setattr(image_tools, "_save_to_gcs", save)
        monkeypatch.setattr(image_tools.config, "image_render_concurrency", 1)
        self.ctx = FakeToolContext(
            {
                "gcs_folder": "f",
                "agent_output_dir": "d",
                "final_visual_concepts": {"visual_concepts": concepts},
                **state,
            }
        )

    def go(self):
        return asyncio.run(image_tools.generate_image(self.ctx))


# --- CV-3: artifact keys ------------------------------------------------------------


@pytest.mark.usefixtures("image_qa_off")
def test_names_differing_only_in_punctuation_get_distinct_keys(monkeypatch):
    run = _Run(monkeypatch, [_concept("Game Day!"), _concept("Game Day")])
    run.go()
    images = run.ctx.state["generated_images"]
    assert set(images) == {"Game Day!", "Game Day"}  # records keep concept names
    assert images["Game Day!"]["artifact_key"] == "Game_Day.png"
    assert images["Game Day"]["artifact_key"] == "Game_Day_2.png"
    assert images["Game Day!"]["gcs_uri"] != images["Game Day"]["gcs_uri"]
    assert run.uploads == {"Game_Day.png": b"img1", "Game_Day_2.png": b"img2"}
    assert run.ctx.state["_generated_artifact_keys"] == [
        "Game_Day.png",
        "Game_Day_2.png",
    ]


def test_unique_artifact_keys_suffixes_in_concept_order():
    from creative_agent.gcs_tools import unique_artifact_keys

    assert unique_artifact_keys(["A b", "A b!", "A-b", "A b?", "C"]) == [
        "A_b.png",
        "A_b_2.png",
        "Ab.png",
        "A_b_3.png",
        "C.png",
    ]
    # a suffixed key never takes a later concept's own key
    assert unique_artifact_keys(["X", "X!", "X 2"]) == [
        "X.png",
        "X_3.png",
        "X_2.png",
    ]


# --- CV-5: render fingerprint -------------------------------------------------------


@pytest.mark.usefixtures("image_qa_off")
def test_unchanged_concepts_skip_a_second_render(monkeypatch):
    run = _Run(monkeypatch, [_concept("A"), _concept("B")])
    run.go()
    assert len(run.renders) == 2
    result = run.go()
    assert len(run.renders) == 2  # idempotent
    assert "already generated" in result["message"]


@pytest.mark.usefixtures("image_qa_off")
def test_changed_concepts_render_again(monkeypatch):
    run = _Run(monkeypatch, [_concept("A"), _concept("B")])
    run.go()
    first = run.ctx.state["_images_generated"]
    run.ctx.state["final_visual_concepts"] = {
        "visual_concepts": [_concept("A"), _concept("B", "PROMPT B: a new scene.")]
    }
    run.go()
    assert len(run.renders) == 4
    assert run.ctx.state["_images_generated"] != first
    assert set(run.ctx.state["generated_images"]) == {"A", "B"}


@pytest.mark.usefixtures("image_qa_off")
def test_a_new_person_reference_renders_again(monkeypatch):
    run = _Run(monkeypatch, [_concept("A")])
    run.go()
    run.ctx.state["person_reference"] = {
        "uri": "gs://b/person-refs/a-0123456789/me.jpg",
        "consent_id": "c1234567",
    }
    run.go()
    assert len(run.renders) == 2


@pytest.mark.usefixtures("image_qa_off")
def test_legacy_boolean_marker_still_skips(monkeypatch):
    run = _Run(monkeypatch, [_concept("A")], _images_generated=True)
    result = run.go()
    assert run.renders == []
    assert result["status"] == "success"


def test_render_fingerprint_is_stable_and_truthy():
    concepts = {"visual_concepts": [_concept("A", casts_person_reference=True)]}
    fp = image_tools.render_fingerprint(concepts, None)
    assert fp and isinstance(fp, str)
    assert fp == image_tools.render_fingerprint(concepts, None)
    uncast = {"visual_concepts": [_concept("A", casts_person_reference=False)]}
    assert fp != image_tools.render_fingerprint(uncast, None)
    person = {"uri": "gs://b/person-refs/a/me.jpg", "consent_id": "c1"}
    assert fp != image_tools.render_fingerprint(concepts, person)


# --- IC-2: the timely cap at render time -------------------------------------------


@pytest.mark.usefixtures("image_qa_off")
def test_render_reapplies_the_timely_cap(monkeypatch):
    monkeypatch.setattr(image_tools.config, "max_timely_concepts", 1)
    concepts = [_concept(f"T{i}", uses_trend_state=True) for i in range(3)]
    run = _Run(
        monkeypatch,
        concepts,
        trend_situation={
            "timely_ok": True,
            "phase": "upcoming",
            "valid_until": "2026-10-16T23:59:59-04:00",
        },
    )
    run.go()
    images = run.ctx.state["generated_images"]
    timely = [name for name, rec in images.items() if rec.get("timely") is True]
    assert timely == ["T0"]


# --- CV-8: concurrent uploads -------------------------------------------------------


@pytest.mark.usefixtures("image_qa_off")
def test_uploads_run_concurrently_bounded_and_records_stay_in_concept_order(
    monkeypatch,
):
    """Kept images upload in parallel (at most UPLOAD_CONCURRENCY at once), start
    in concept order, and generated_images / _generated_artifact_keys keep
    concept order whatever order the uploads finish in."""
    import threading
    import time

    names = [f"N{i}" for i in range(1, 7)]
    run = _Run(monkeypatch, [_concept(n) for n in names])
    lock = threading.Lock()
    calls: list[str] = []
    in_flight = 0
    peak = 0

    def save(*, tool_context, image_bytes, filename, metadata=None):
        nonlocal in_flight, peak
        with lock:
            calls.append(filename)
            in_flight += 1
            peak = max(peak, in_flight)
        # earlier uploads finish last, so completion order is reversed
        time.sleep(0.05 * (len(names) - len(calls) + 1))
        with lock:
            in_flight -= 1
        return f"gs://b/f/d/{filename}"

    monkeypatch.setattr(image_tools, "_save_to_gcs", save)
    run.go()

    keys = [f"{n}.png" for n in names]
    assert image_tools.UPLOAD_CONCURRENCY == 4
    assert peak == image_tools.UPLOAD_CONCURRENCY
    assert calls == keys  # started in concept order
    assert list(run.ctx.state["generated_images"]) == names
    assert run.ctx.state["_generated_artifact_keys"] == keys


# --- Nits: upload content type ------------------------------------------------------


@pytest.mark.usefixtures("image_qa_off")
def test_upload_content_type_follows_the_models_mime(monkeypatch):
    """A JPEG from the image model is uploaded as image/jpeg (was always
    image/png); a PNG keeps _save_to_gcs's image/png default."""
    run = _Run(monkeypatch, [_concept("A"), _concept("B")])
    mimes = iter(["image/jpeg", "image/png"])

    async def render(contents, aspect_ratio, image_config=None):
        return b"img", next(mimes)

    seen: dict[str, str | None] = {}

    def save(*, tool_context, image_bytes, filename, metadata=None, **kw):
        seen[filename] = kw.get("content_type")
        return f"gs://b/f/d/{filename}"

    monkeypatch.setattr(image_tools, "_render_image", render)
    monkeypatch.setattr(image_tools, "_save_to_gcs", save)
    run.go()
    assert seen == {"A.png": "image/jpeg", "B.png": None}


def test_save_to_gcs_uploads_with_the_given_content_type(monkeypatch):
    from types import SimpleNamespace

    from creative_agent import gcs_tools

    uploads = []

    class _Blob:
        metadata = None

        def upload_from_string(self, data, content_type=None):
            uploads.append(content_type)

    client = SimpleNamespace(bucket=lambda _n: SimpleNamespace(blob=lambda _p: _Blob()))
    monkeypatch.setattr(gcs_tools, "_get_gcs_client", lambda: client)
    ctx = FakeToolContext({"gcs_folder": "f", "agent_output_dir": "d"})
    gcs_tools._save_to_gcs(ctx, b"x", "a.png", content_type="image/jpeg")
    gcs_tools._save_to_gcs(ctx, b"x", "b.png")
    assert uploads == ["image/jpeg", "image/png"]
