"""Golden recording of both judge requests (system + user content, in order).

Pins the exact text the ad-copy and visual judges receive for a fixed set of
fixture creatives (base, timely, safer-angle, person cast, no brief, no trend,
prompt-only), so a refactor of ``creative_eval/prompts.py`` / ``evaluate.py``
can prove it changed nothing the judge sees. A deliberate wording change
updates the golden (regenerate with ``UPDATE_JUDGE_GOLDEN=1``) and bumps
``JUDGE_VERSION`` (CLAUDE.md → Evaluation).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from creative_eval.config import EvalConfig
from creative_eval.dimensions import AD_COPY_GATES, VISUAL_GATES
from creative_eval.evaluate import evaluate_ad_copy, evaluate_visual_concept
from tests._eval_fakes import verdict_dicts

GOLDEN = Path(__file__).parent / "fixtures" / "judge_prompts_golden.json"
BUCKET = "tt-bucket"
RENDER = f"gs://{BUCKET}/out/hero.png"
PHOTO = f"gs://{BUCKET}/person-refs/me-abc/face.jpg"

CAMPAIGN = {
    "brand": "ACME",
    "target_product": "Rocket Skates",
    "target_audience": "runners <18-34>",
    "key_selling_points": "fast; light",
    "target_search_trend": "Texas vs Oklahoma",
}
BRIEF = {
    "single_minded_proposition": "Rocket Skates make every run a highlight.",
    "reasons_to_believe": [{"claim": "Carbon frame"}, "Sealed bearings"],
    "mandatories": ["Rocket Skates", "the ACME logo"],
    "avoid": ["team logos", "AI-generated look"],
    "trend_bridge": {
        "fit_score": 4,
        "fit_mode": "cultural",
        "bridge": "Game-day energy meets speed.",
        "risks": ["mocking the losing team"],
        "safer_angle": "the band's joyful legacy",
    },
    "brand": {
        "tone_of_voice": "playful",
        "do_not": ["no swearing"],
        "distinctive_assets": ["red swoosh"],
    },
    "angles": [{"angle_id": "A1", "name": "Speed", "route": "fastest fan"}],
}
SITUATION = {
    "phase": "upcoming",
    "event_name": "Texas vs Oklahoma",
    "event_start": "2026-10-16",
    "event_end": "",
    "outcome": "none",
    "outcome_summary": "",
    "as_of": "2026-10-15T09:00:00-04:00",
    "valid_until": "2026-10-16T23:59:59-04:00",
    "tone_rule": "anticipation, never predict the score",
    "timely_ok": True,
    "source_ids": ["src-1", "src-9"],
}
SOURCES = {"src-1": {"url": "https://example.com/2026/10/14/preview"}}
AD_COPY = {
    "original_id": 3,
    "angle_id": "A1",
    "headline": "Beat the <GATES> rush",
    "body_text": "Glide to the game.",
    "tone_style": "Playful",
    "trend_connection": "Game day",
    "audience_appeal_rationale": "Fans love speed",
    "social_caption": "Zoom {in}",
    "call_to_action": "Shop now",
    "detailed_performance_rationale": "Short and punchy",
}
CONCEPT = {
    "ad_copy_id": 3,
    "concept_name": "Hero",
    "visual_style": "Candid 35mm film photo",
    "aspect_ratio": "",
    "trend": "Texas vs Oklahoma",
    "trend_reference": "stadium",
    "trend_motif": "",
    "brand_cue": "red swoosh",
    "markets_product": "skates on feet",
    "audience_appeal": "a",
    "selection_rationale": "s",
    "headline": "Beat the rush",
    "social_caption": "Zoom",
    "call_to_action": "Shop now",
    "concept_summary": "A fan skates to the stadium.",
    "image_generation_prompt": "the person in the person reference image skating",
    "angle_id": "A1",
}
QA_FAILED = {"passed": False, "failures": ["product <small>", "text"]}


def _body(gates: tuple[str, ...], kind: str) -> str:
    return json.dumps(
        {
            "score": {
                "verdicts": verdict_dicts(kind, 9),
                "strengths": [],
                "improvements": [],
                "gates": [{"gate": g, "passed": True, "note": "n"} for g in gates],
            }
        }
    )


def _client(gates: tuple[str, ...], kind: str) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=_body(gates, kind))
    return client


def _part(part: Any) -> dict[str, str]:
    if isinstance(part, str):
        return {"text": part}
    file_data = getattr(part, "file_data", None)
    if file_data is not None and file_data.file_uri:
        return {"uri": file_data.file_uri, "mime": file_data.mime_type or ""}
    return {"text": part.text or ""}


def _request(client: MagicMock) -> dict[str, Any]:
    kwargs = client.models.generate_content.call_args.kwargs
    contents = kwargs["contents"]
    parts = contents if isinstance(contents, list) else [contents]
    return {
        "system": kwargs["config"].system_instruction,
        "contents": [_part(p) for p in parts],
    }


def _ad(name: str, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    ad_copy = kwargs.pop("ad_copy", AD_COPY)
    campaign = kwargs.pop("campaign", CAMPAIGN)
    client = _client(AD_COPY_GATES, "ad")
    evaluate_ad_copy(ad_copy, campaign, EvalConfig(), client=client, **kwargs)
    return f"ad_copy/{name}", _request(client)


def _visual(name: str, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    concept = kwargs.pop("concept", CONCEPT)
    campaign = kwargs.pop("campaign", CAMPAIGN)
    client = _client(VISUAL_GATES, "visual")
    evaluate_visual_concept(concept, campaign, EvalConfig(), client=client, **kwargs)
    return f"visual/{name}", _request(client)


def _record() -> dict[str, dict[str, Any]]:
    timely_copy = {**AD_COPY, "uses_trend_state": True}
    no_trend = {**CAMPAIGN, "target_search_trend": ""}
    cases = [
        _ad("base", brief=BRIEF),
        _ad("no_brief", brief=None),
        _ad("no_trend", brief=BRIEF, campaign=no_trend),
        _ad(
            "timely",
            ad_copy=timely_copy,
            brief=BRIEF,
            trend_situation=SITUATION,
            sources=SOURCES,
        ),
        _ad(
            "safer_angle",
            brief=BRIEF,
            safer_angle=True,
            sensitive_reason="a <fatal> crash",
        ),
        _visual("base", brief=BRIEF, image={"gcs_uri": RENDER, "qa": QA_FAILED}),
        _visual("prompt_only_no_brief", brief=None, image=None),
        _visual(
            "no_trend",
            brief=BRIEF,
            campaign=no_trend,
            image={"gcs_uri": RENDER, "qa": {"passed": True}},
        ),
        _visual(
            "person_cast",
            brief=BRIEF,
            image={"gcs_uri": RENDER, "cast": True, "qa": None},
            person_photo=PHOTO,
        ),
        _visual(
            "timely_paired",
            brief=BRIEF,
            image={"gcs_uri": RENDER, "qa": None},
            trend_situation=SITUATION,
            sources=SOURCES,
            paired_copy_timely=True,
        ),
        _visual(
            "safer_angle_no_brief",
            brief=None,
            image=None,
            safer_angle=True,
            sensitive_reason="a crash",
        ),
    ]
    return dict(cases)


@pytest.fixture(autouse=True)
def _bucket(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_STORAGE_BUCKET", BUCKET)


def test_judge_requests_match_golden():
    recorded = _record()
    if os.environ.get("UPDATE_JUDGE_GOLDEN"):
        GOLDEN.write_text(json.dumps(recorded, indent=1, ensure_ascii=False) + "\n")
    golden = json.loads(GOLDEN.read_text())
    assert recorded.keys() == golden.keys()
    for name, request in recorded.items():
        assert request == golden[name], f"judge request changed: {name}"
