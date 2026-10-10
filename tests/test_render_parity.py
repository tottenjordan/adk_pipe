"""Behaviour parity for the ``render_concept`` refactor of ``generate_image``.

Each scenario drives ``generate_image`` with a fake image client and scripted QA
verdicts, and records every image-model call (contents + config), every QA call,
the uploads and the resulting state. The recording is compared with a golden
file captured from the pre-refactor implementation
(``tests/fixtures/render_parity.json``), so the refactor keeps the exact same
requests, re-renders, uploads and state. Regenerate deliberately with
``UPDATE_RENDER_PARITY=1`` (only when a behaviour change is intended).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from creative_agent import image_qa, image_tools
from creative_agent.image_qa import ImageQAResult
from tests._fakes import FakeToolContext, noop_async

GOLDEN = Path(__file__).parent / "fixtures" / "render_parity.json"
PERSON_URI = "gs://b/person-refs/alice-0123456789/me.jpg"


def _verdict(**overrides) -> ImageQAResult:
    base = {
        "product_visible": True,
        "motif_visible": True,
        "brand_cue_visible": True,
        "text_expected": False,
        "text_exact": True,
        "text_legible": True,
        "gibberish_text": False,
        "unrequested_logos": False,
        "artifacts": False,
        "unsafe": False,
        "issues": [],
    }
    base.update(overrides)
    return ImageQAResult(**base)


def _concept(name: str, prompt: str, **extra) -> dict:
    return {
        "concept_name": name,
        "image_generation_prompt": prompt,
        "trend_motif": "a lottery ball",
        "brand_cue": "bird inlays",
        "visual_style": extra.pop("visual_style", "Risograph poster"),
        **extra,
    }


def _part_record(part: Any) -> Any:
    if isinstance(part, str):
        return part
    inline = part.inline_data
    return {"bytes": inline.data.decode("latin-1"), "mime": inline.mime_type}


def _contents_record(contents: Any) -> Any:
    if isinstance(contents, str):
        return contents
    return [_part_record(p) for p in contents]


def _image_response(data: bytes) -> Any:
    inline = SimpleNamespace(data=data, mime_type="image/png")
    part = SimpleNamespace(inline_data=inline, thought=False)
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=[part]), finish_reason=types.FinishReason.STOP
    )
    return SimpleNamespace(candidates=[candidate], prompt_feedback=None)


def _blocked_response() -> Any:
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=[]), finish_reason=types.FinishReason.IMAGE_SAFETY
    )
    return SimpleNamespace(candidates=[candidate], prompt_feedback=None)


def _record(monkeypatch, scenario: dict) -> dict:
    # Grouped per concept: the order across concepts depends on how the pipelined
    # QA threads interleave, the order within a concept does not.
    calls: dict[str, list[dict]] = {}
    qa_calls: dict[str, list[dict]] = {}
    uploads: list[list[str]] = []
    block = set(scenario.get("block_person_for", ()))
    verdicts = {k: list(v) for k, v in scenario.get("verdicts", {}).items()}

    def generate_content(**kwargs):
        image_config = kwargs["config"].image_config
        text = kwargs["contents"]
        text = text if isinstance(text, str) else text[0]
        name = text.split(":", 1)[0]
        mine = calls.setdefault(name, [])
        mine.append(
            {
                "model": kwargs["model"],
                "contents": _contents_record(kwargs["contents"]),
                "modalities": kwargs["config"].response_modalities,
                "aspect_ratio": image_config.aspect_ratio,
                "image_size": image_config.image_size,
                "person_generation": image_config.person_generation,
            }
        )
        if image_config.person_generation and name in block:
            return _blocked_response()
        return _image_response(f"img-{name}-{len(mine)}".encode())

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    monkeypatch.setattr(image_tools, "_get_genai_client", lambda: client)
    monkeypatch.setattr(image_tools.asyncio, "sleep", noop_async)

    def fake_save(*, tool_context, image_bytes, filename, metadata=None):
        uploads.append([filename, image_bytes.decode("latin-1")])
        return f"gs://b/{filename}"

    def fake_download(bucket, obj):
        if obj.startswith("person-refs/") and scenario.get("photo_missing"):
            raise RuntimeError("gone")
        if obj.endswith("missing.png"):
            raise RuntimeError("gone")
        return f"BYTES:{obj}".encode()

    monkeypatch.setattr(image_tools, "_save_to_gcs", fake_save)
    monkeypatch.setattr(image_tools, "_download_blob", fake_download)
    monkeypatch.setattr(image_tools.config, "GCS_BUCKET_NAME", "b")
    monkeypatch.setattr(image_tools.config, "image_gen_model", "image-model")
    monkeypatch.setattr(image_tools.config, "image_size", "2K")
    monkeypatch.setattr(image_tools.config, "image_qa_enabled", scenario["qa"])
    monkeypatch.setattr(image_tools.config, "image_qa_max_rerenders", 1)
    monkeypatch.setattr(
        image_tools.config, "image_qa_max_rerenders_per_run", scenario["budget"]
    )
    monkeypatch.setattr(image_tools.config, "image_qa_model", "qa-model")
    monkeypatch.setattr(image_qa, "_get_qa_client", lambda: "qa-client")

    def fake_inspect(image_bytes, mime, concept, **kwargs):
        person = kwargs.get("person_image")
        qa_calls.setdefault(concept["concept_name"], []).append(
            {
                "image": image_bytes.decode("latin-1"),
                "concept": concept["concept_name"],
                "casts": concept.get("casts_person_reference"),
                "prompt": concept["image_generation_prompt"],
                "brand": kwargs["brand"],
                "product": kwargs["target_product"],
                "model": kwargs["model"],
                "logo": kwargs.get("has_logo_reference", False),
                "strictness": list(kwargs.get("strictness", ())),
                "person_image": (
                    None if person is None else [person[0].decode("latin-1"), person[1]]
                ),
            }
        )
        queue = verdicts.get(concept["concept_name"]) or []
        return _verdict(**queue.pop(0)) if queue else _verdict()

    monkeypatch.setattr(image_qa, "inspect_image", fake_inspect)

    ctx = FakeToolContext(
        {
            "gcs_folder": "f",
            "agent_output_dir": "d",
            "brand": "PRS",
            "target_product": "PRS SE guitar",
            "final_visual_concepts": {"visual_concepts": scenario["concepts"]},
            **scenario.get("state", {}),
        }
    )
    result = asyncio.run(image_tools.generate_image(ctx))
    state = {
        k: ctx.state[k]
        for k in (
            "generated_images",
            "_generated_artifact_keys",
            "image_qa__issues",
            "image_qa__unavailable",
            "person_reference_rejected",
            "person_reference__issues",
        )
        if k in ctx.state
    }
    return {
        "result": result,
        "calls": calls,
        "qa_calls": qa_calls,
        "uploads": uploads,
        "state": json.loads(json.dumps(state, default=str)),
    }


_CAST_STYLE = "Candid 35mm film photo"
_CAST_PROMPT = (
    "a candid 35mm film photo of a skater, the person in the person reference "
    "image laughing with a PRS SE guitar beside a lottery ball"
)

SCENARIOS: dict[str, dict] = {
    "references_strictness_override_rerender_budget": {
        "qa": True,
        "budget": 1,
        "state": {
            "reference_images": [
                {"uri": "gs://b/refs/product.png", "role": "product"},
                {"uri": "gs://b/refs/logo.png", "role": "logo"},
                {"uri": "gs://b/refs/missing.png", "role": "style"},
            ],
            "visual_aspect_ratio": "4:5",
            "rating_strictness": ["unwanted_logo", "product_not_visible"],
        },
        "concepts": [
            _concept("C1", "C1: a PRS SE guitar beside a lottery ball"),
            _concept(
                "C2", "C2: a PRS SE guitar on a lottery ticket", aspect_ratio="1:1"
            ),
            _concept("C3", "C3: a PRS SE guitar under lottery lights"),
        ],
        "verdicts": {
            "C1": [{"product_visible": False}, {"product_visible": True}],
            "C2": [{"artifacts": True, "issues": ["warped neck"]}],
        },
    },
    "person_cast_block_fallback_and_likeness": {
        "qa": True,
        "budget": 4,
        "state": {
            "person_reference": {"uri": PERSON_URI, "consent_id": "consent-1"},
            "reference_images": [{"uri": "gs://b/refs/product.png", "role": "product"}],
        },
        "block_person_for": ["P2"],
        "concepts": [
            _concept(
                "P1",
                "P1: " + _CAST_PROMPT,
                visual_style=_CAST_STYLE,
                casts_person_reference=True,
                person_casting_reason="solo hero",
                aspect_ratio="9:16",
            ),
            _concept(
                "P2",
                "P2: " + _CAST_PROMPT,
                visual_style=_CAST_STYLE,
                casts_person_reference=True,
                person_casting_reason="solo hero",
            ),
            _concept("P3", "P3: a flat cartoon PRS SE guitar"),
        ],
        "verdicts": {
            "P1": [{"person_cast": True, "person_likeness": False}, {}],
        },
    },
    "qa_off_photo_unavailable": {
        "qa": False,
        "budget": 2,
        "photo_missing": True,
        "state": {"person_reference": {"uri": PERSON_URI, "consent_id": "consent-1"}},
        "concepts": [
            _concept(
                "U1",
                "U1: " + _CAST_PROMPT,
                visual_style=_CAST_STYLE,
                casts_person_reference=True,
                person_casting_reason="solo hero",
            ),
            _concept("U2", "U2: a PRS SE guitar beside a lottery ball"),
        ],
    },
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_generate_image_matches_pre_refactor_recording(monkeypatch, name):
    got = _record(monkeypatch, SCENARIOS[name])
    golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if os.environ.get("UPDATE_RENDER_PARITY") == "1":
        golden[name] = got
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n")
    assert got == golden[name]
