"""Tests for Track c.2: `cast` role, VLM reference descriptor,
`composition_mode` schema/gate, per-concept reference filtering, and judge fallback.
"""

from __future__ import annotations

import asyncio
import json
import re
import types as pytypes
from pathlib import Path
from typing import Any, get_args

import pytest
from google.genai import types
from pydantic import ValidationError

import creative_agent
from creative_agent import concept_guard, image_qa, image_tools, reference_descriptor
from creative_agent.references import (
    MAX_REFERENCE_IMAGES,
    REFERENCE_ROLES,
    reference_roles_summary,
    resolve_references,
)
from creative_agent.schemas import (
    CompositionMode,
    VisualConcept,
    VisualConceptCritique,
    VisualConceptFinal,
)
from creative_eval import evaluate as eval_module
from creative_eval.config import EvalConfig
from creative_eval.dimensions import AD_COPY_GATES, VISUAL_GATES

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. 4-Role Reference Parsing & Python <-> TypeScript Parity
# ---------------------------------------------------------------------------


class TestReferenceRolesAndParity:
    def test_four_roles_and_max_count(self):
        assert MAX_REFERENCE_IMAGES == 4
        assert REFERENCE_ROLES == ("product", "logo", "style", "cast")

    def test_resolves_all_four_roles_in_order_and_caps_at_four(self):
        state = {
            "reference_image_uri": "gs://b/prod.png",
            "reference_image_role": "product",
            "reference_images": [
                {"uri": "gs://b/logo.png", "role": "logo"},
                {"uri": "gs://b/style.png", "role": "style"},
                {"uri": "gs://b/cast.png", "role": " CAST "},
                {"uri": "gs://b/extra.png", "role": "style"},
            ],
        }
        assert resolve_references(state) == [
            ("gs://b/prod.png", "product"),
            ("gs://b/logo.png", "logo"),
            ("gs://b/style.png", "style"),
            ("gs://b/cast.png", "cast"),
        ]
        assert reference_roles_summary(state) == "product, logo, style, cast"

    def test_python_and_typescript_reference_constants_stay_in_lockstep(self):
        ts_source = (REPO_ROOT / "frontend/src/lib/reference-images.ts").read_text(
            encoding="utf-8"
        )
        max_match = re.search(
            r"export\s+const\s+MAX_REFERENCE_IMAGES\s*=\s*(\d+)\s*;", ts_source
        )
        assert max_match is not None
        assert int(max_match.group(1)) == MAX_REFERENCE_IMAGES

        roles_match = re.search(
            r"export\s+const\s+REFERENCE_ROLES\s*=\s*\[([^\]]+)\]\s*as\s+const\s*;",
            ts_source,
        )
        assert roles_match is not None
        ts_roles = tuple(re.findall(r'"([^"]+)"', roles_match.group(1)))
        assert ts_roles == REFERENCE_ROLES

        fieldset_source = (
            REPO_ROOT / "frontend/src/components/new-run/reference-images-fieldset.tsx"
        ).read_text(encoding="utf-8")
        assert '<SelectItem value="cast">' in fieldset_source

        stepper_source = (
            REPO_ROOT / "frontend/src/components/guided-brief-stepper.tsx"
        ).read_text(encoding="utf-8")
        assert "<ReferenceImagesFieldset" in stepper_source


# ---------------------------------------------------------------------------
# 2. Parallel VLM Product & Cast Descriptor
# ---------------------------------------------------------------------------


class TestReferenceDescriptor:
    def test_sanitizer_strips_braces_surrogates_and_clamps(self):
        dirty = (
            "  - Bottle: {matte-cobalt} cylinder\ud800 with   silver cap \n\n\n"
            "  - Label OCR: 'HYDRA {ZERO}' on front surface  "
        )
        cleaned = reference_descriptor.sanitize_reference_description(dirty)
        assert "{" not in cleaned and "}" not in cleaned
        assert "\ud800" not in cleaned
        assert "matte-cobalt" in cleaned and "HYDRA ZERO" in cleaned
        assert "\n\n" not in cleaned

        huge = "A" * (reference_descriptor.MAX_DESCRIPTION_CHARS + 250)
        clamped = reference_descriptor.sanitize_reference_description(huge)
        assert len(clamped) == reference_descriptor.MAX_DESCRIPTION_CHARS

    @pytest.mark.parametrize(
        "state",
        [
            {},
            {"reference_image_uri": ""},
            {
                "reference_images": [
                    {"uri": "gs://b/logo.png", "role": "logo"},
                    {"uri": "gs://b/style.png", "role": "style"},
                ]
            },
        ],
    )
    def test_zero_rpc_fast_path_without_product_or_cast(self, monkeypatch, state):
        fetch_calls: list[str] = []
        vlm_calls: list[Any] = []

        monkeypatch.setattr(
            image_tools,
            "fetch_reference_image",
            lambda uri: fetch_calls.append(uri),
        )
        monkeypatch.setattr(
            reference_descriptor,
            "_get_genai_client",
            lambda: vlm_calls.append(1),
        )

        ctx = pytypes.SimpleNamespace(state=dict(state))
        result = asyncio.run(reference_descriptor.describe_reference_assets(ctx))
        assert result is None
        assert ctx.state["reference_product_description"] == ""
        assert fetch_calls == []
        assert vlm_calls == []

    def test_extracts_sanitized_description_for_product_and_cast(self, monkeypatch):
        fetched_uris: list[str] = []

        def fake_fetch(uri: str) -> types.Part | None:
            fetched_uris.append(uri)
            return types.Part.from_bytes(data=uri.encode(), mime_type="image/png")

        vlm_requests: list[dict[str, Any]] = []

        class _FakeModels:
            def generate_content(self, **kwargs):
                vlm_requests.append(kwargs)
                return pytypes.SimpleNamespace(
                    text=(
                        "- Product: frosted amber glass dropper {30ml}, printed label "
                        "'SOLARIS C+' on front face only.\n"
                        "- Cast: woman in 30s, curly dark hair, linen blazer {tan}."
                    )
                )

        monkeypatch.setattr(image_tools, "fetch_reference_image", fake_fetch)
        monkeypatch.setattr(
            reference_descriptor,
            "_get_genai_client",
            lambda: pytypes.SimpleNamespace(models=_FakeModels()),
        )

        state = {
            "reference_images": [
                {"uri": "gs://b/prod.png", "role": "product"},
                {"uri": "gs://b/style.png", "role": "style"},
                {"uri": "gs://b/talent.png", "role": "cast"},
            ]
        }
        ctx = pytypes.SimpleNamespace(state=dict(state))
        asyncio.run(reference_descriptor.describe_reference_assets(ctx))

        # Only product and cast are fetched; style is ignored by the VLM descriptor.
        assert set(fetched_uris) == {"gs://b/prod.png", "gs://b/talent.png"}
        assert len(vlm_requests) == 1
        assert vlm_requests[0]["model"] == creative_agent.config.config.worker_model

        desc = ctx.state["reference_product_description"]
        assert "{" not in desc and "}" not in desc
        assert "SOLARIS C+" in desc and "30ml" in desc and "linen blazer" in desc

    def test_fails_open_when_all_fetches_return_none(self, monkeypatch):
        vlm_called = False

        def boom():
            nonlocal vlm_called
            vlm_called = True
            raise AssertionError("VLM must not be called when fetches fail")

        monkeypatch.setattr(image_tools, "fetch_reference_image", lambda uri: None)
        monkeypatch.setattr(reference_descriptor, "_get_genai_client", boom)

        delta = asyncio.run(
            reference_descriptor.describe_reference_assets_delta(
                {"reference_image_uri": "gs://b/missing.png"}
            )
        )
        assert delta == {"reference_product_description": ""}
        assert vlm_called is False

    def test_fails_open_on_vlm_exception_and_timeout(self, monkeypatch):
        import time

        monkeypatch.setattr(
            image_tools,
            "fetch_reference_image",
            lambda uri: types.Part.from_bytes(data=b"img", mime_type="image/png"),
        )

        class _BrokenModels:
            def generate_content(self, **kwargs):
                raise RuntimeError("503 service unavailable")

        monkeypatch.setattr(
            reference_descriptor,
            "_get_genai_client",
            lambda: pytypes.SimpleNamespace(models=_BrokenModels()),
        )
        delta_err = asyncio.run(
            reference_descriptor.describe_reference_assets_delta(
                {"reference_image_uri": "gs://b/p.png"}
            )
        )
        assert delta_err == {"reference_product_description": ""}

        def slow_generate(fetched):
            time.sleep(1.5)
            return "too late"

        monkeypatch.setattr(
            reference_descriptor, "_generate_description", slow_generate
        )

        async def run_with_short_timeout():
            return await reference_descriptor.describe_reference_assets_delta(
                {"reference_image_uri": "gs://b/p.png"}, timeout=-0.95
            )

        delta_timeout = asyncio.run(run_with_short_timeout())
        assert delta_timeout == {"reference_product_description": ""}

    def test_fails_open_when_reference_image_fetch_stalls(self, monkeypatch):
        import time

        vlm_called = False

        def slow_fetch(uri: str):
            time.sleep(1.5)
            return types.Part.from_bytes(data=b"img", mime_type="image/png")

        def unexpected_vlm(fetched):
            nonlocal vlm_called
            vlm_called = True
            return "should not run"

        monkeypatch.setattr(image_tools, "fetch_reference_image", slow_fetch)
        monkeypatch.setattr(
            reference_descriptor, "_generate_description", unexpected_vlm
        )

        delta = asyncio.run(
            reference_descriptor.describe_reference_assets_delta(
                {"reference_image_uri": "gs://b/stalled.png"}, timeout=-0.95
            )
        )
        assert delta == {"reference_product_description": ""}
        assert vlm_called is False


# ---------------------------------------------------------------------------
# 3. CompositionMode Schema & Deterministic concept_guard Checks
# ---------------------------------------------------------------------------


def _make_concept(
    ad_copy_id: int = 1,
    prompt: str = "Clean studio packshot of Hydra Bottle on volcanic slate.",
    *,
    composition_mode: str = "product_in_scene",
    trend_motif: str = "volcanic slate",
) -> dict[str, Any]:
    return {
        "ad_copy_id": ad_copy_id,
        "concept_name": f"Concept {ad_copy_id}",
        "headline": f"Headline {ad_copy_id}",
        "call_to_action": "Shop now",
        "visual_style": "Clean studio product shot",
        "trend_motif": trend_motif,
        "brand_cue": "cobalt cap",
        "composition_mode": composition_mode,
        "image_generation_prompt": prompt,
    }


_SAMPLE_COPIES = {
    "ad_copies": [
        {
            "original_id": i,
            "tone_style": "Aspirational",
            "headline": f"Headline {i}",
            "body_text": "Body.",
            "trend_connection": "Trend.",
            "audience_justification": "Aud.",
            "social_caption": "Cap.",
            "call_to_action": "Shop now",
            "detailed_performance_rationale": "Why.",
        }
        for i in range(1, 5)
    ]
}


class TestCompositionModeSchemaAndGuard:
    def test_composition_mode_literal_and_defaults(self):
        assert creative_agent.CompositionMode is CompositionMode
        assert get_args(CompositionMode) == (
            "product_hero",
            "product_in_scene",
            "cast_with_product",
            "lifestyle_context",
            "graphic_lockup",
        )
        vc = VisualConcept(
            ad_copy_id=1,
            concept_name="Hero",
            trend_visual_link="link",
            concept_summary="sum",
            image_generation_prompt="prompt",
        )
        assert vc.composition_mode == "product_in_scene"

        vcc = VisualConceptCritique(
            ad_copy_id=1,
            concept_name="Hero",
            trend_visual_link="link",
            concept_summary="sum",
            image_generation_prompt="prompt",
            critique_summary="ok",
        )
        assert vcc.composition_mode == "product_in_scene"

        vcf = VisualConceptFinal(
            ad_copy_id=1,
            concept_name="Hero",
            trend="t",
            trend_reference="tr",
            markets_product="mp",
            audience_appeal="aa",
            selection_rationale="sr",
            headline="h",
            social_caption="sc",
            call_to_action="cta",
            concept_summary="sum",
            image_generation_prompt="prompt",
        )
        assert vcf.composition_mode == "product_in_scene"

        with pytest.raises(ValidationError):
            VisualConcept(
                ad_copy_id=1,
                concept_name="Bad",
                trend_visual_link="link",
                concept_summary="sum",
                composition_mode="drone_flyover",  # type: ignore[arg-type]
                image_generation_prompt="prompt",
            )

    def test_product_hero_flags_human_hands_and_models_without_false_positives(self):
        contaminated = [
            _make_concept(
                1,
                "A woman's hands holding the Hydra Bottle over volcanic slate.",
                composition_mode="product_hero",
            ),
            _make_concept(
                2,
                "A smiling spokesperson posing next to the Hydra Bottle on volcanic slate.",
                composition_mode="product_hero",
            ),
        ]
        issues = concept_guard.concept_issues(contaminated, _SAMPLE_COPIES)
        assert set(issues) == {"1", "2"}
        assert "product_hero" in str(issues["1"][0])
        assert "product_hero" in str(issues["2"][0])

        clean_false_positive_traps = [
            _make_concept(
                1,
                "Handcrafted cobalt bottle, hand-carved cap, model number X200, "
                "framed in 3:4 portrait orientation beside a brushed watch face on "
                "volcanic slate, no human hands or people.",
                composition_mode="product_hero",
            ),
            _make_concept(
                2,
                "Second-hand vinyl sleeve and handlebar grip beside Hydra Bottle on volcanic slate.",
                composition_mode="product_in_scene",
            ),
            _make_concept(
                3,
                "Studio packshot of Hydra Bottle on volcanic slate, zero humans, "
                "faces, or hands holding the product; no men or women in frame; "
                "without wrists or palms.",
                composition_mode="product_hero",
            ),
            _make_concept(
                4,
                "A brass stand holding the guitar with nickel tremolo arm on volcanic "
                "slate, velvet case cradling the body.",
                composition_mode="product_hero",
            ),
        ]
        assert (
            concept_guard.concept_issues(clean_false_positive_traps, _SAMPLE_COPIES)
            == {}
        )

        real_human_contamination = [
            _make_concept(
                1,
                "Close-up of a woman's hands holding the bottle on volcanic slate.",
                composition_mode="product_hero",
            ),
            _make_concept(
                2,
                "A smiling creator with the guitar on volcanic slate.",
                composition_mode="product_hero",
            ),
        ]
        flagged = concept_guard.concept_issues(real_human_contamination, _SAMPLE_COPIES)
        assert set(flagged) == {"1", "2"}

    def test_cast_with_product_flags_ungrounded_reference_cast_claim(self):
        claiming_ref = [
            _make_concept(
                1,
                "Feature the reference person holding Hydra Bottle beside volcanic slate.",
                composition_mode="cast_with_product",
            )
        ]
        # Flagged when no cast reference exists.
        issues_no_cast = concept_guard.concept_issues(
            claiming_ref, _SAMPLE_COPIES, reference_roles=("product", "style")
        )
        assert "1" in issues_no_cast
        assert "cast_with_product" in str(issues_no_cast["1"][0])

        # Passes when "cast" IS in reference_roles (either tuple or comma summary string).
        assert (
            concept_guard.concept_issues(
                claiming_ref,
                _SAMPLE_COPIES,
                reference_roles=("product", "cast"),
            )
            == {}
        )
        assert (
            concept_guard.concept_issues(
                claiming_ref,
                _SAMPLE_COPIES,
                reference_roles="product, cast",
            )
            == {}
        )

        # Generic talent without claiming an uploaded reference passes even without cast reference.
        generic_talent = [
            _make_concept(
                1,
                "An energetic trail runner holding Hydra Bottle beside volcanic slate.",
                composition_mode="cast_with_product",
            )
        ]
        assert (
            concept_guard.concept_issues(
                generic_talent, _SAMPLE_COPIES, reference_roles=("product",)
            )
            == {}
        )

    def test_set_level_composition_mode_diversity_and_style_preference_exemption(self):
        monotone = [
            _make_concept(
                i,
                f"Off-centre Hydra Bottle scene {i} on volcanic slate.",
                composition_mode="product_in_scene",
            )
            for i in range(1, 5)
        ]
        issues = concept_guard.concept_issues(monotone, _SAMPLE_COPIES)
        assert list(issues) == ["4"]
        assert "composition_mode" in str(issues["4"][0])

        # Exempt when user explicitly pinned visual_style_preference.
        assert (
            concept_guard.concept_issues(
                monotone,
                _SAMPLE_COPIES,
                visual_style_preference="Diecut sticker",
            )
            == {}
        )

        # Passes when at least 2 composition modes appear across the 4 concepts.
        diverse = [dict(c) for c in monotone]
        diverse[0]["composition_mode"] = "product_hero"
        assert concept_guard.concept_issues(diverse, _SAMPLE_COPIES) == {}


# ---------------------------------------------------------------------------
# 4. Per-Concept Reference Filtering in image_tools & QA Context
# ---------------------------------------------------------------------------


class TestPerConceptReferenceFiltering:
    def test_filter_references_for_composition_pure(self):
        refs = [
            ("product", "PART_PROD"),
            ("cast", "PART_CAST"),
            ("style", "PART_STYLE"),
        ]
        missing = ["logo", "cast"]

        for excluded_mode in ("product_hero", "graphic_lockup", " PRODUCT_HERO "):
            f_refs, f_missing = image_tools.filter_references_for_composition(
                refs, missing, excluded_mode
            )
            assert f_refs == [("product", "PART_PROD"), ("style", "PART_STYLE")]
            assert f_missing == ["logo"]

        for kept_mode in (
            "cast_with_product",
            "product_in_scene",
            "lifestyle_context",
            None,
            "",
        ):
            f_refs, f_missing = image_tools.filter_references_for_composition(
                refs, missing, kept_mode
            )
            assert f_refs == refs
            assert f_missing == missing

    def test_generate_image_strips_cast_for_hero_and_lockup_incl_qa_rerender(
        self, monkeypatch
    ):
        # Stub GCS blob download & upload and genai render + QA inspect.
        def fake_download(bucket: str, obj: str) -> bytes:
            if obj == "missing_cast.png":
                raise RuntimeError("404")
            return obj.encode()

        monkeypatch.setattr(image_tools, "_download_blob", fake_download)
        monkeypatch.setattr(
            image_tools,
            "_save_to_gcs",
            lambda tool_context, image_bytes, filename, **kw: f"gs://b/out/{filename}",
        )
        monkeypatch.setattr(image_tools.config, "image_qa_enabled", True)
        monkeypatch.setattr(image_tools.config, "image_qa_max_rerenders", 1)
        monkeypatch.setattr(image_tools.config, "image_qa_max_rerenders_per_run", 2)

        render_calls: list[Any] = []

        async def fake_render(contents, aspect_ratio: str):
            render_calls.append(contents)
            return b"PNGDATA", "image/png"

        monkeypatch.setattr(image_tools, "_render_image", fake_render)

        qa_descriptions: list[str] = []
        qa_attempt = {"Hero": 0}

        def fake_inspect(
            image_bytes,
            mime,
            concept,
            *,
            brand,
            target_product,
            client,
            model,
            has_logo_reference=False,
            strictness=(),
            reference_description="",
        ):
            qa_descriptions.append(reference_description)
            name = concept["concept_name"]
            if name == "Hero" and qa_attempt["Hero"] == 0:
                qa_attempt["Hero"] += 1
                # Trigger one QA re-render on Hero to verify re-render ALSO omits cast!
                return image_qa.ImageQAResult(
                    product_visible=True,
                    motif_visible=True,
                    text_expected=False,
                    gibberish_text=False,
                    unrequested_logos=False,
                    artifacts=True,
                    unsafe=False,
                    issues=["melted cap on bottle"],
                )
            return image_qa.ImageQAResult(
                product_visible=True,
                motif_visible=True,
                text_expected=False,
                gibberish_text=False,
                unrequested_logos=False,
                artifacts=False,
                unsafe=False,
                issues=[],
            )

        monkeypatch.setattr(image_qa, "inspect_image", fake_inspect)
        monkeypatch.setattr(image_qa, "_get_qa_client", lambda: object())

        class _Ctx:
            def __init__(self, state):
                self.state = state

            async def save_artifact(self, filename, artifact):
                return None

        ctx = _Ctx(
            {
                "gcs_folder": "f",
                "agent_output_dir": "out",
                "brand": "Hydra",
                "target_product": "Hydra Bottle",
                "reference_product_description": "Cobalt cylinder bottle; curly-haired spokesperson.",
                "reference_images": [
                    {"uri": "gs://b/product.png", "role": "product"},
                    {"uri": "gs://b/cast.png", "role": "cast"},
                    {"uri": "gs://b/missing_cast.png", "role": "cast"},
                ],
                "final_visual_concepts": {
                    "visual_concepts": [
                        {
                            "concept_name": "Hero",
                            "composition_mode": "product_hero",
                            "trend_motif": "slate",
                            "image_generation_prompt": "Pure packshot of Hydra Bottle.",
                        },
                        {
                            "concept_name": "Lockup",
                            "composition_mode": "graphic_lockup",
                            "trend_motif": "slate",
                            "image_generation_prompt": "Bold graphic emblem of Hydra Bottle.",
                        },
                        {
                            "concept_name": "Talent",
                            "composition_mode": "cast_with_product",
                            "trend_motif": "slate",
                            "image_generation_prompt": "Talent holding Hydra Bottle.",
                        },
                    ]
                },
            }
        )

        res = asyncio.run(image_tools.generate_image(ctx))
        assert res["status"] == "success"
        # 4 renders total: Hero (initial) + Lockup + Talent + Hero (QA re-render).
        assert len(render_calls) == 4

        hero_calls = [
            c for c in render_calls if "Pure packshot of Hydra Bottle." in c[0]
        ]
        assert len(hero_calls) == 2  # initial + QA re-render
        for call in hero_calls:
            assert "(cast)" not in call[0]
            assert "cast reference image is unavailable" not in call[0]
            assert [p.inline_data.data for p in call[1:]] == [b"product.png"]

        lockup_call = next(c for c in render_calls if "Bold graphic emblem" in c[0])
        assert "(cast)" not in lockup_call[0]
        assert [p.inline_data.data for p in lockup_call[1:]] == [b"product.png"]

        talent_call = next(
            c for c in render_calls if "Talent holding Hydra Bottle." in c[0]
        )
        assert "Reference image 2 (cast):" in talent_call[0]
        assert [p.inline_data.data for p in talent_call[1:]] == [
            b"product.png",
            b"cast.png",
        ]
        assert all("Cobalt cylinder bottle" in d for d in qa_descriptions)


# ---------------------------------------------------------------------------
# 5. creative_eval Legacy Fallback for Missing composition_mode
# ---------------------------------------------------------------------------


class TestEvalLegacyCompositionModeFallback:
    def test_legacy_visual_concept_without_composition_mode_evaluates_cleanly(self):
        cfg = EvalConfig()
        captured_prompts: list[str] = []
        valid_judge_json = json.dumps(
            {
                "ad_copy_id": 1,
                "concept_name": "Legacy Concept",
                "score": {
                    "gates": [
                        {"gate": g, "passed": True, "note": "ok"} for g in VISUAL_GATES
                    ],
                    "verdicts": [
                        {
                            "dimension": dim,
                            "score": 8,
                            "verdict": "pass",
                            "rationale": "strong",
                        }
                        for dim in cfg.visual_dimensions
                    ],
                    "strengths": ["clear"],
                    "improvements": [],
                },
            }
        )

        class _FakeJudgeModels:
            def generate_content(self, **kwargs):
                captured_prompts.append(str(kwargs["contents"]))
                return pytypes.SimpleNamespace(text=valid_judge_json)

        fake_client = pytypes.SimpleNamespace(models=_FakeJudgeModels())
        campaign_ctx = {
            "brand": "Hydra",
            "target_product": "Hydra Bottle",
            "target_audience": "Runners",
            "key_selling_points": "Insulated",
            "target_search_trend": "trail running",
        }
        # Deliberately omit `composition_mode` to simulate a legacy visual concept dict.
        legacy_concept = {
            "ad_copy_id": 1,
            "concept_name": "Legacy Concept",
            "visual_style": "Candid 35mm film photo",
            "trend": "trail running",
            "trend_reference": "trail",
            "markets_product": "shows bottle",
            "audience_appeal": "runners",
            "selection_rationale": "fit",
            "headline": "Run Cool",
            "social_caption": "Stay cold.",
            "call_to_action": "Shop now",
            "concept_summary": "summary",
            "image_generation_prompt": "A trail runner with Hydra Bottle at dawn.",
        }

        evaluation = eval_module.evaluate_visual_concept(
            legacy_concept, campaign_ctx, cfg, client=fake_client
        )
        assert evaluation.score.passed is True
        assert len(captured_prompts) == 1
        assert "Composition Mode: product_in_scene" in captured_prompts[0]

    def test_evaluate_stamps_copy_and_visual_ids_from_input_over_judge_echo(self):
        cfg = EvalConfig()
        # Judge echoes original_id=1 and paraphrased headline/tone_style; code must stamp input fields.
        ad_judge_json = json.dumps(
            {
                "original_id": 1,
                "headline": "Judge Paraphrased Headline",
                "tone_style": "Judge Invented Tone",
                "score": {
                    "gates": [
                        {"gate": g, "passed": True, "note": "ok"} for g in AD_COPY_GATES
                    ],
                    "verdicts": [
                        {
                            "dimension": dim,
                            "score": 9,
                            "verdict": "pass",
                            "rationale": "good",
                        }
                        for dim in cfg.ad_copy_dimensions
                    ],
                    "strengths": ["strong"],
                    "improvements": [],
                },
            }
        )
        vis_judge_json = json.dumps(
            {
                "ad_copy_id": 1,
                "concept_name": "Midnight Riff!",
                "score": {
                    "gates": [
                        {"gate": g, "passed": True, "note": "ok"} for g in VISUAL_GATES
                    ],
                    "verdicts": [
                        {
                            "dimension": dim,
                            "score": 8,
                            "verdict": "pass",
                            "rationale": "good",
                        }
                        for dim in cfg.visual_dimensions
                    ],
                    "strengths": ["strong"],
                    "improvements": [],
                },
            }
        )

        campaign_ctx = {
            "brand": "Hydra",
            "target_product": "Hydra Bottle",
            "target_audience": "Runners",
            "key_selling_points": "Insulated",
            "target_search_trend": "trail running",
        }
        ad_copy = {
            "original_id": "7",
            "tone_style": "Urgent/FOMO",
            "headline": "Exact Copy Headline",
            "body_text": "Hydra Bottle keeps water icy.",
            "call_to_action": "Grab yours",
            "social_caption": "Stay icy.",
            "trend_connection": "Trail heat.",
            "audience_appeal_rationale": "Runners.",
            "detailed_performance_rationale": "FOMO.",
            "angle_id": "A2",
        }
        ad_client = pytypes.SimpleNamespace(
            models=pytypes.SimpleNamespace(
                generate_content=lambda **kw: pytypes.SimpleNamespace(
                    text=ad_judge_json
                )
            )
        )
        ad_eval = eval_module.evaluate_ad_copy(
            ad_copy, campaign_ctx, cfg, client=ad_client
        )
        assert ad_eval.original_id == 7
        assert ad_eval.headline == "Exact Copy Headline"
        assert ad_eval.tone_style == "Urgent/FOMO"

        vis_concept = {
            "ad_copy_id": 4.0,
            "concept_name": "Midnight Riff",
            "visual_style": "Comic panel",
            "composition_mode": "product_hero",
            "trend": "trail running",
            "trend_reference": "trail",
            "markets_product": "shows bottle",
            "audience_appeal": "runners",
            "selection_rationale": "fit",
            "headline": "Exact Copy Headline",
            "social_caption": "Stay icy.",
            "call_to_action": "Grab yours",
            "concept_summary": "summary",
            "image_generation_prompt": "Hydra Bottle packshot.",
        }
        vis_client = pytypes.SimpleNamespace(
            models=pytypes.SimpleNamespace(
                generate_content=lambda **kw: pytypes.SimpleNamespace(
                    text=vis_judge_json
                )
            )
        )
        vis_eval = eval_module.evaluate_visual_concept(
            vis_concept, campaign_ctx, cfg, client=vis_client
        )
        assert vis_eval.ad_copy_id == 4
        assert vis_eval.concept_name == "Midnight Riff"
