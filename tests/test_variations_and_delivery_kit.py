"""Unit & endpoint tests for controlled single-axis variations and Delivery Kit (.zip)."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import zipfile
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import errors as genai_errors
from google.genai import types

from agent_common.idempotency import stable_row_id
from creative_agent.gcs_tools import artifact_key_for
from creative_agent.variations import (
    AXIS_SPECS,
    VARIATION_AXES,
    VariationAxis,
    apply_frozen_invariants,
    default_target_values,
    draft_variation_candidates,
    generate_variations_for_session,
    mint_variation_concept_name,
    repair_variation_copy_and_concept,
)
from creative_eval.dimensions import DIMENSION_LABELS
from creative_eval.schemas import (
    AdCopyEvaluation,
    CreativeScore,
    EvalVerdict,
    GateResult,
    VisualConceptEvaluation,
)
from runserver import changelog, delivery_kit, ratings, variations
from runserver.authz import AuthzMode, UserAuthzMiddleware
from runserver.experiments import snapshot_arms
from runserver.ratings_store import InMemoryRatingsStore

ALICE = "alice@example.com"
BOB = "bob@example.com"
ALLOWED_BUCKET = "allowed-campaign-bucket"
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png-payload"
AD_COPY_DIMENSIONS = tuple(DIMENSION_LABELS)[:6]
VISUAL_DIMENSIONS = tuple(DIMENSION_LABELS)[6:]


def _make_ad_eval(
    original_id: int, headline: str, score_10: int = 8
) -> AdCopyEvaluation:
    verdicts = [
        EvalVerdict(
            dimension=dim,
            score=score_10,
            verdict="pass",
            rationale=f"Strong {dim}",
        )
        for dim in AD_COPY_DIMENSIONS
    ]
    return AdCopyEvaluation(
        original_id=original_id,
        headline=headline,
        tone_style="Aspirational",
        angle_id="A1",
        score=CreativeScore(
            overall_score=round(score_10 / 10, 3),
            passed=score_10 >= 7,
            verdicts=verdicts,
            strengths=list(AD_COPY_DIMENSIONS[:2]),
            improvements=[],
            gates=[GateResult(gate="product_named", passed=True, note="Named")],
            gates_passed=True,
        ),
    )


def _make_vis_eval(
    ad_copy_id: int, concept_name: str, score_10: int = 9
) -> VisualConceptEvaluation:
    verdicts = [
        EvalVerdict(
            dimension=dim,
            score=score_10,
            verdict="pass",
            rationale=f"Clear {dim}",
        )
        for dim in VISUAL_DIMENSIONS
    ]
    return VisualConceptEvaluation(
        ad_copy_id=ad_copy_id,
        concept_name=concept_name,
        visual_style="Photoreal / editorial",
        angle_id="A1",
        image_judged=True,
        score=CreativeScore(
            overall_score=round(score_10 / 10, 3),
            passed=score_10 >= 7,
            verdicts=verdicts,
            strengths=list(VISUAL_DIMENSIONS[:2]),
            improvements=[],
            gates=[GateResult(gate="product_visible", passed=True, note="Visible")],
            gates_passed=True,
        ),
    )


def _sample_base_copy(original_id: int = 1) -> dict[str, Any]:
    return {
        "original_id": original_id,
        "tone_style": "Aspirational",
        "angle_id": "A1",
        "headline": f"Play PRS Custom {original_id} Under Tour Lights",
        "body_text": f"PRS Custom {original_id} brings resonant sustain to late-night sets.",
        "trend_connection": "Riffing on live stadium guitar solos.",
        "audience_appeal_rationale": "Speaks to gigging guitarists chasing stage tone.",
        "social_caption": f"Dial in stage sustain with PRS Custom {original_id}. #PRSGuitars",
        "typicality": 0.35,
        "call_to_action": "Explore the guitar now",
        "brief_checks": [],
        "detailed_performance_rationale": "Pairs authentic stage craft with trend momentum.",
    }


def _sample_base_concept(idx: int = 1) -> dict[str, Any]:
    name = f"Stadium Encore {idx}"
    return {
        "ad_copy_id": idx,
        "concept_name": name,
        "trend": "stadium guitar encore",
        "trend_reference": "Captures the viral stadium encore spotlight moment.",
        "markets_product": f"Highlights the carved maple top of PRS Custom {idx}.",
        "audience_appeal": "Resonates with touring players and bedroom musicians.",
        "selection_rationale": "High visual contrast and instant product recognition.",
        "headline": f"Play PRS Custom {idx} Under Tour Lights",
        "social_caption": f"Dial in stage sustain with PRS Custom {idx}. #PRSGuitars",
        "call_to_action": "Explore the guitar now",
        "concept_summary": "Editorial stage shot of the guitar illuminated by encore beams.",
        "visual_style": "Photoreal / editorial",
        "aspect_ratio": "9:16",
        "trend_motif": "stadium spotlight beams",
        "brand_cue": "PRS bird fretboard inlays",
        "angle_id": "A1",
        "composition_mode": "product_in_scene",
        "image_generation_prompt": (
            f"Editorial photo of PRS Custom {idx} with PRS bird fretboard inlays "
            "resting on a vintage amp under stadium spotlight beams, clean negative space."
        ),
    }


def _sample_session_state(num_concepts: int = 4) -> dict[str, Any]:
    copies = [_sample_base_copy(i + 1) for i in range(num_concepts)]
    concepts = [_sample_base_concept(i + 1) for i in range(num_concepts)]
    gen_images = {
        c["concept_name"]: {
            "gcs_uri": f"gs://{ALLOWED_BUCKET}/out/run1/{artifact_key_for(c['concept_name'])}",
            "artifact_key": artifact_key_for(c["concept_name"]),
            "attempts": 1,
            "qa": {"passed": True, "failures": []},
        }
        for c in concepts
    }
    ad_evals = [_make_ad_eval(c["original_id"], c["headline"]) for c in copies]
    vis_evals = [
        _make_vis_eval(vc["ad_copy_id"], vc["concept_name"]) for vc in concepts
    ]
    return {
        "brand": "PRS Guitars",
        "target_product": "PRS Custom 24",
        "target_audience": "Gigging guitarists",
        "key_selling_points": "Carved maple top, bird inlays",
        "target_search_trends": "stadium guitar encore",
        "gcs_bucket_name": ALLOWED_BUCKET,
        "gcs_folder": "out",
        "agent_output_dir": "run1",
        "creative_brief": {
            "single_minded_proposition": "Stage-ready sustain that cuts through every encore.",
            "reasons_to_believe": ["Carved maple top", "Signature bird inlays"],
            "mandatories": ["Name PRS Custom 24"],
            "avoid": ["cheap knockoff"],
        },
        "creative_brief_md": "# Creative Brief\nStage-ready sustain for every encore.",
        "final_report_with_citations": "# Research Report\nCited cultural insights [1].",
        "research_report_gcs_uri": f"gs://{ALLOWED_BUCKET}/out/run1/research_report_with_citations.pdf",
        "creative_gallery_gcs_uri": f"gs://{ALLOWED_BUCKET}/out/run1/creative_portfolio_gallery.html",
        "ad_copy_critique": {"ad_copies": copies},
        "final_visual_concepts": {"visual_concepts": concepts},
        "generated_images": gen_images,
        "_generated_artifact_keys": [
            artifact_key_for(c["concept_name"]) for c in concepts
        ],
        "creative_evaluation_report": {
            "brand": "PRS Guitars",
            "target_product": "PRS Custom 24",
            "target_search_trend": "stadium guitar encore",
            "ad_copy_evaluations": [e.model_dump() for e in ad_evals],
            "visual_concept_evaluations": [e.model_dump() for e in vis_evals],
            "summary": {
                "total_ad_copies": num_concepts,
                "ad_copies_passed": num_concepts,
                "avg_ad_copy_score": 0.8,
                "total_visual_concepts": num_concepts,
                "visual_concepts_passed": num_concepts,
                "avg_visual_score": 0.9,
                "overall_pass_rate": 1.0,
                "weakest_dimensions": [],
                "gates_pass_rate": 1.0,
            },
            "warnings": [],
            "judge_model": "gemini-3.1-pro-preview",
            "passing_threshold": 0.7,
            "brief_used": True,
        },
    }


# ---------------------------------------------------------------------------
# 1. Pure single-axis frozen invariant tests across all 6 axes
# ---------------------------------------------------------------------------


def _hostile_llm_draft() -> dict[str, Any]:
    """Hostile mock LLM output attempting to mutate EVERY field simultaneously."""
    return {
        "target_value": "HostileTarget",
        "headline": "DRIFTED HEADLINE FOR PRS Custom 24",
        "body_text": "DRIFTED BODY TEXT FOR PRS Custom 24.",
        "social_caption": "DRIFTED SOCIAL CAPTION FOR PRS Custom 24 #Drift",
        "call_to_action": "Buy drifted offer today",
        "tone_style": "Humorous",
        "visual_style": "2D flat / vector cartoon",
        "composition_mode": "graphic_lockup",
        "markets_product": "DRIFTED MARKETS PRODUCT",
        "audience_appeal": "DRIFTED AUDIENCE APPEAL",
        "trend_motif": "DRIFTED TREND MOTIF",
        "brand_cue": "DRIFTED BRAND CUE",
        "aspect_ratio": "1:1",
        "angle_id": "A99",
        "trend": "DRIFTED TREND",
        "trend_reference": "DRIFTED TREND REFERENCE",
        "concept_summary": "Updated summary describing the single-axis change.",
        "selection_rationale": "Updated causal hypothesis for bandit lift.",
        "image_generation_prompt": (
            "Updated prompt featuring PRS Custom 24 with PRS bird fretboard inlays "
            "under stadium spotlight beams."
        ),
    }


@pytest.mark.parametrize("axis", VARIATION_AXES)
def test_frozen_invariants_enforced_across_all_six_axes(axis: VariationAxis) -> None:
    base_concept = _sample_base_concept(1)
    base_copy = _sample_base_copy(1)
    draft = _hostile_llm_draft()
    spec = AXIS_SPECS[axis]

    target_value = (
        "product_hero"
        if axis == "composition_mode"
        else "2D flat / vector cartoon"
        if axis == "visual_style"
        else "PRS SE Hollowbody"
        if axis == "sibling_sku"
        else "Tokyo, Japan"
    )
    minted_name = mint_variation_concept_name(
        base_concept["concept_name"], axis, target_value, [base_concept["concept_name"]]
    )

    concept, new_copy = apply_frozen_invariants(
        axis,
        base_concept,
        base_copy,
        draft,
        target_value=target_value,
        minted_name=minted_name,
        base_concept_index=0,
        new_ad_copy_id=5 if spec.mutates_copy else None,
    )

    # Lineage metadata must always be present and exact.
    assert concept["variation_of"] == base_concept["concept_name"]
    assert concept["variation_base_index"] == 0
    assert concept["variation_axis"] == axis
    assert concept["variation_value"] == target_value
    assert concept["concept_name"] == minted_name

    # Every non-mutated field on VisualConcept MUST equal base_concept byte-for-byte.
    lineage_keys = {
        "variation_of",
        "variation_base_index",
        "variation_axis",
        "variation_value",
    }
    for key, base_val in base_concept.items():
        if key not in spec.mutated_concept_fields and key not in lineage_keys:
            assert concept[key] == base_val, (
                f"Axis {axis!r} leaked mutation on frozen concept field {key!r}: "
                f"{concept[key]!r} != {base_val!r}"
            )

    if not spec.mutates_copy:
        assert new_copy is None
        assert concept["ad_copy_id"] == base_concept["ad_copy_id"]
        assert concept["headline"] == base_concept["headline"]
        assert concept["social_caption"] == base_concept["social_caption"]
        assert concept["call_to_action"] == base_concept["call_to_action"]
    else:
        assert new_copy is not None
        assert new_copy["original_id"] == 5
        assert concept["ad_copy_id"] == 5
        for key, base_val in base_copy.items():
            if key not in spec.mutated_copy_fields:
                assert new_copy[key] == base_val, (
                    f"Axis {axis!r} leaked mutation on frozen copy field {key!r}: "
                    f"{new_copy[key]!r} != {base_val!r}"
                )

    # Axis-specific spot checks:
    if axis == "cta_hook":
        assert new_copy is not None
        assert new_copy["headline"] == draft["headline"]
        assert new_copy["call_to_action"] == draft["call_to_action"]
        assert new_copy["body_text"] == base_copy["body_text"]
        assert new_copy["social_caption"] == base_copy["social_caption"]
        assert new_copy["tone_style"] == base_copy["tone_style"]
        assert concept["visual_style"] == base_concept["visual_style"]
        assert concept["composition_mode"] == base_concept["composition_mode"]
    elif axis == "visual_style":
        assert concept["visual_style"] == "2D flat / vector cartoon"
        assert concept["composition_mode"] == base_concept["composition_mode"]
    elif axis == "composition_mode":
        assert concept["composition_mode"] == "product_hero"
        assert concept["visual_style"] == base_concept["visual_style"]


def test_mint_variation_concept_name_never_collides_across_punctuation_or_batches() -> (
    None
):
    existing = {"Neon: Drop!", "Neon Drop"}
    minted: list[str] = []
    for batch in range(3):
        for idx, val in enumerate(["Tokyo!", "Tokyo?"]):
            name = mint_variation_concept_name(
                "Neon: Drop!",
                "locale_market",
                val,
                existing,
                index=idx + batch,
            )
            assert name not in existing
            existing.add(name)
            minted.append(name)

    artifact_keys = [artifact_key_for(n) for n in minted]
    row_ids = [stable_row_id("sess-123", n) for n in minted]
    assert len(set(artifact_keys)) == len(minted)
    assert len(set(row_ids)) == len(minted)


def test_repair_variation_enforces_gates_without_breaking_frozen_fields() -> None:
    base_concept = _sample_base_concept(1)
    base_copy = _sample_base_copy(1)

    # 1. product_hero human contamination is scrubbed cleanly while freezing copy.
    concept_hero, copy_hero = apply_frozen_invariants(
        "composition_mode",
        base_concept,
        base_copy,
        {
            "composition_mode": "product_hero",
            "image_generation_prompt": "A smiling model holding the guitar in her hands.",
        },
        target_value="product_hero",
        minted_name="Stadium Encore 1 Var Comp product hero 1",
        base_concept_index=0,
    )
    repaired_concept, repaired_copy, _ = repair_variation_copy_and_concept(
        "composition_mode",
        concept_hero,
        copy_hero,
        base_concept=base_concept,
        base_copy=base_copy,
        target_product="PRS Custom 24",
        target_value="product_hero",
        brand="PRS Guitars",
        trend="stadium guitar encore",
        brief={"avoid": ["cheap knockoff"]},
    )
    assert repaired_copy is None
    assert "hands" not in repaired_concept["image_generation_prompt"].lower()
    assert "PRS Custom 24" in repaired_concept["image_generation_prompt"]
    assert repaired_concept["headline"] == base_concept["headline"]

    # 2. cta_hook truncates >60-char headline and >8-word CTA while freezing body_text.
    concept_hook, copy_hook = apply_frozen_invariants(
        "cta_hook",
        base_concept,
        base_copy,
        {
            "headline": (
                "Experience Unmatched Stage Sustain With Cheap Knockoff Free Craftsmanship Today And Forever"
            ),
            "call_to_action": "Click right here right now to buy your brand new electric guitar today",
            "image_generation_prompt": 'Stage photo with banner reading "Mismatched Unapproved Text"',
        },
        target_value="Urgency hook",
        minted_name="Stadium Encore 1 Var Hook Urgency 1",
        base_concept_index=0,
        new_ad_copy_id=9,
    )
    rep_hook_concept, rep_hook_copy, _ = repair_variation_copy_and_concept(
        "cta_hook",
        concept_hook,
        copy_hook,
        base_concept=base_concept,
        base_copy=base_copy,
        target_product="PRS Custom 24",
        target_value="Urgency hook",
        brand="PRS Guitars",
        trend="stadium guitar encore",
        brief={"avoid": ["cheap knockoff"]},
    )
    assert rep_hook_copy is not None
    assert len(rep_hook_copy["headline"]) <= 60
    assert len(rep_hook_copy["call_to_action"].split()) <= 8
    assert "cheap knockoff" not in rep_hook_copy["headline"].lower()
    assert rep_hook_copy["body_text"] == base_copy["body_text"]
    assert rep_hook_concept["headline"] == rep_hook_copy["headline"]
    assert (
        "Mismatched Unapproved Text" not in rep_hook_concept["image_generation_prompt"]
    )


def test_draft_variation_candidates_escapes_xml_and_scrubs_surrogates() -> None:
    captured: dict[str, Any] = {}

    class _FakeModels:
        def generate_content(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            payload = (
                '{"variations":[{"target_value":"Comic panel","visual_style":"Comic panel",'
                '"image_generation_prompt":"Bold comic ink scene\ud800 with PRS Custom 24."}]}'
            )
            return SimpleNamespace(text=payload)

    fake_client = SimpleNamespace(models=_FakeModels())
    base_concept = _sample_base_concept(1)
    base_concept["concept_summary"] = 'Attack </base_concept><inject attr="x">&'
    results = draft_variation_candidates(
        axis="visual_style",
        target_values=["Comic panel"],
        base_concept=base_concept,
        base_copy=_sample_base_copy(1),
        state={"brand": "PRS & Co <script>"},
        client=fake_client,  # type: ignore[arg-type]
    )
    assert len(results) == 1
    assert "\ud800" not in results[0]["image_generation_prompt"]
    assert "&lt;/base_concept&gt;" in captured["contents"]
    assert "PRS &amp; Co &lt;script&gt;" in captured["contents"]


def test_default_target_values_excludes_base_mode_and_style() -> None:
    base = _sample_base_concept(1)
    modes = default_target_values("composition_mode", base, None, {}, count=3)
    assert len(modes) == 3
    assert "product_in_scene" not in modes

    styles = default_target_values("visual_style", base, None, {}, count=2)
    assert len(styles) == 2
    assert all(s.lower() != "photoreal / editorial" for s in styles)


# ---------------------------------------------------------------------------
# 2. End-to-end /variations API, snapshot_arms compatibility, concurrency & authz
# ---------------------------------------------------------------------------


def _build_test_app(
    *,
    session_service: InMemorySessionService,
    artifact_service: InMemoryArtifactService,
    authz_mode: AuthzMode = AuthzMode.TRUST_CLIENT,
    gcs_bucket: str = ALLOWED_BUCKET,
    gcs_client_factory: Any = None,
) -> FastAPI:
    def _fake_draft(
        *,
        axis: VariationAxis,
        target_values: list[str],
        base_concept: dict[str, Any],
        base_copy: dict[str, Any] | None,
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        out = []
        for val in target_values:
            prod = (
                val if axis == "sibling_sku" else state.get("target_product", "Product")
            )
            out.append(
                {
                    "target_value": val,
                    "headline": f"{prod}: {val}"[:58],
                    "body_text": f"Experience {prod} tailored for {val}.",
                    "social_caption": f"{prod} for {val} #TourReady",
                    "call_to_action": "Try it live today",
                    "tone_style": "Emotional/Authentic",
                    "visual_style": val if axis == "visual_style" else "DRIFT_STYLE",
                    "composition_mode": val
                    if axis == "composition_mode"
                    else "graphic_lockup",
                    "markets_product": f"Showcases {prod} clearly.",
                    "audience_appeal": f"Tailored for {val}.",
                    "concept_summary": f"Controlled {axis} variation ({val}).",
                    "selection_rationale": f"Tests {axis}={val} lift.",
                    "image_generation_prompt": (
                        f"Scene featuring {prod} with PRS bird fretboard inlays "
                        f"under stadium spotlight beams ({val})."
                    ),
                }
            )
        return out

    async def _fake_render(contents: Any, aspect_ratio: str) -> tuple[bytes, str]:
        await asyncio.sleep(0.01)
        return PNG_BYTES, "image/png"

    def _fake_eval_copy(
        copy_item: dict[str, Any], ctx: dict[str, Any]
    ) -> AdCopyEvaluation:
        return _make_ad_eval(int(copy_item["original_id"]), copy_item["headline"], 9)

    def _fake_eval_visual(
        concept: dict[str, Any],
        ctx: dict[str, Any],
        image_rec: Any,
    ) -> VisualConceptEvaluation:
        return _make_vis_eval(int(concept["ad_copy_id"]), concept["concept_name"], 8)

    async def _runner(**kwargs: Any) -> Any:
        return await generate_variations_for_session(
            **kwargs,
            draft_fn=_fake_draft,
            render_fn=_fake_render,
            eval_copy_fn=_fake_eval_copy,
            eval_visual_fn=_fake_eval_visual,
        )

    variations.configure(
        session_service=session_service,
        artifact_service=artifact_service,
        authz_mode=authz_mode,
        variation_runner=_runner,
    )
    changelog.configure(
        session_service=session_service,
        artifact_service=artifact_service,
        authz_mode=authz_mode,
    )
    delivery_kit.configure(
        session_service=session_service,
        artifact_service=artifact_service,
        authz_mode=authz_mode,
        gcs_bucket=gcs_bucket,
        gcs_client_factory=gcs_client_factory,
    )

    app = FastAPI()
    app.include_router(variations.router)
    app.include_router(changelog.router)
    app.include_router(delivery_kit.router)
    app.add_middleware(
        UserAuthzMiddleware,
        mode=authz_mode,
        caller_ok=lambda auth: auth == "Bearer proxy",
    )
    return app


def test_variations_endpoint_updates_state_artifacts_changelog_and_bandit_snapshot_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Disable real GCS writes during variation generation by clearing config.GCS_BUCKET_NAME
    from creative_agent.config import config as ca_config

    monkeypatch.setattr(ca_config, "GCS_BUCKET_NAME", "")

    async def _run() -> None:
        session_svc = InMemorySessionService()
        artifact_svc = InMemoryArtifactService()
        await session_svc.create_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-var-1",
            state=_sample_session_state(4),
        )

        app = _build_test_app(
            session_service=session_svc, artifact_service=artifact_svc
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-var-1/variations",
                json={
                    "base_concept_index": 0,
                    "axis": "cta_hook",
                    "count": 2,
                    "target_values": ["Urgency hook", "Curiosity hook"],
                },
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["added_indices"] == [4, 5]
            assert len(data["variations"]) == 2
            assert len(data["new_ad_copies"]) == 2

            # Verify summary recomputed for 6 copies and 6 visual concepts.
            report = data["creative_evaluation_report"]
            assert report["summary"]["total_ad_copies"] == 6
            assert report["summary"]["total_visual_concepts"] == 6
            assert len(report["ad_copy_evaluations"]) == 6
            assert len(report["visual_concept_evaluations"]) == 6

            # Verify ADK artifact_service saved both rendered variation PNGs.
            saved_names = await artifact_svc.list_artifact_keys(
                app_name="creative_agent",
                user_id=ALICE,
                session_id="sess-var-1",
            )
            for v in data["variations"]:
                assert artifact_key_for(v["concept_name"]) in saved_names

            # Verify /changelog surfaces kind="variations".
            cl_resp = await client.get(
                f"/runs/creative_agent/{ALICE}/sess-var-1/changelog"
            )
            assert cl_resp.status_code == 200
            cl_entries = cl_resp.json()["entries"]
            var_entries = [e for e in cl_entries if e["kind"] == "variations"]
            assert len(var_entries) == 1
            assert "2 controlled variation(s)" in var_entries[0]["label"]
            assert "CTA / Hook" in var_entries[0]["detail"]

        # Direct bandit compatibility test: snapshot_arms on base [0] + variations [4, 5]!
        updated_session = await session_svc.get_session(
            app_name="creative_agent", user_id=ALICE, session_id="sess-var-1"
        )
        assert updated_session is not None
        arms = snapshot_arms(
            updated_session.state,
            "sess-var-1",
            [0, 4, 5],
            default_bucket=ALLOWED_BUCKET,
        )
        assert len(arms) == 3
        assert len({arm["creativeId"] for arm in arms}) == 3
        for arm in arms:
            assert arm["imageUri"] is not None
            assert arm["imageUri"].startswith(f"gs://{ALLOWED_BUCKET}/out/run1/")
            assert arm["overallScore"] is not None and arm["overallScore"] > 0.7
            # All 12 dimensions + 2 overall scores = 14 entries in arm["scores"]
            assert len(arm["scores"]) == 14

    asyncio.run(_run())


def test_variations_concurrency_lock_prevents_lost_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from creative_agent.config import config as ca_config

    monkeypatch.setattr(ca_config, "GCS_BUCKET_NAME", "")

    async def _run() -> None:
        session_svc = InMemorySessionService()
        artifact_svc = InMemoryArtifactService()
        await session_svc.create_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-race",
            state=_sample_session_state(4),
        )

        app = _build_test_app(
            session_service=session_svc, artifact_service=artifact_svc
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r1, r2 = await asyncio.gather(
                client.post(
                    f"/runs/creative_agent/{ALICE}/sess-race/variations",
                    json={"base_concept_index": 0, "axis": "visual_style", "count": 2},
                ),
                client.post(
                    f"/runs/creative_agent/{ALICE}/sess-race/variations",
                    json={
                        "base_concept_index": 1,
                        "axis": "composition_mode",
                        "count": 2,
                    },
                ),
            )
            assert r1.status_code == 200
            assert r2.status_code == 200

        final_sess = await session_svc.get_session(
            app_name="creative_agent", user_id=ALICE, session_id="sess-race"
        )
        assert final_sess is not None
        concepts = final_sess.state["final_visual_concepts"]["visual_concepts"]
        assert len(concepts) == 8
        names = [c["concept_name"] for c in concepts]
        assert len(set(names)) == 8

    asyncio.run(_run())


def test_variations_validation_cap_running_and_authz_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from creative_agent.config import config as ca_config

    monkeypatch.setattr(ca_config, "GCS_BUCKET_NAME", "")

    async def _run() -> None:
        session_svc = InMemorySessionService()
        artifact_svc = InMemoryArtifactService()

        # 15 concepts already in session -> count=2 exceeds MAX_CONCEPTS_PER_SESSION=16 (409).
        await session_svc.create_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-cap",
            state=_sample_session_state(15),
        )
        # In-flight running session -> 409.
        running_state = _sample_session_state(4)
        running_state["__run_status"] = "running"
        await session_svc.create_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-running",
            state=running_state,
        )

        app = _build_test_app(
            session_service=session_svc,
            artifact_service=artifact_svc,
            authz_mode=AuthzMode.ENFORCE,
        )
        transport = httpx.ASGITransport(app=app)
        alice_headers = {"authorization": "Bearer proxy", "x-tt-user": ALICE}
        bob_headers = {"authorization": "Bearer proxy", "x-tt-user": BOB}

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # Missing trusted identity -> 401
            r_unauth = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-cap/variations",
                json={"base_concept_index": 0, "axis": "visual_style", "count": 1},
            )
            assert r_unauth.status_code == 401

            # Cross-user path mismatch -> 403
            r_forbidden = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-cap/variations",
                headers=bob_headers,
                json={"base_concept_index": 0, "axis": "visual_style", "count": 1},
            )
            assert r_forbidden.status_code == 403

            # Unowned session -> 404
            r_not_found = await client.post(
                f"/runs/creative_agent/{BOB}/sess-cap/variations",
                headers=bob_headers,
                json={"base_concept_index": 0, "axis": "visual_style", "count": 1},
            )
            assert r_not_found.status_code == 404

            # Non-creative app -> 409
            r_bad_app = await client.post(
                f"/runs/trend_scout/{ALICE}/sess-cap/variations",
                headers=alice_headers,
                json={"base_concept_index": 0, "axis": "visual_style", "count": 1},
            )
            assert r_bad_app.status_code == 409

            # Active running session -> 409
            r_running = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-running/variations",
                headers=alice_headers,
                json={"base_concept_index": 0, "axis": "visual_style", "count": 1},
            )
            assert r_running.status_code == 409

            # Out-of-range base_concept_index -> 400
            r_oob = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-cap/variations",
                headers=alice_headers,
                json={"base_concept_index": 99, "axis": "visual_style", "count": 1},
            )
            assert r_oob.status_code == 400

            # Exceeds 16-concept ceiling -> 409
            r_cap = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-cap/variations",
                headers=alice_headers,
                json={"base_concept_index": 0, "axis": "visual_style", "count": 2},
            )
            assert r_cap.status_code == 409

            # Extra field rejected (extra="forbid") -> 422
            r_extra = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-cap/variations",
                headers=alice_headers,
                json={
                    "base_concept_index": 0,
                    "axis": "visual_style",
                    "unexpected": True,
                },
            )
            assert r_extra.status_code == 422

            # Control character in target_values -> 422
            r_ctrl = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-cap/variations",
                headers=alice_headers,
                json={
                    "base_concept_index": 0,
                    "axis": "visual_style",
                    "target_values": ["Tokyo\nInjected"],
                },
            )
            assert r_ctrl.status_code == 422

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3. Delivery Kit (.zip) contents, GCS bucket confinement, CSV safety & 150 MB cap
# ---------------------------------------------------------------------------


class _FakeBlob:
    def __init__(self, data: bytes | None, *, reported_size: int | None = None) -> None:
        self._data = data
        self.size = (
            reported_size
            if reported_size is not None
            else (len(data) if data is not None else None)
        )
        self.download_calls = 0

    def reload(self) -> None:
        if self._data is None and self.size is None:
            raise FileNotFoundError("404 blob not found")

    def download_as_bytes(self) -> bytes:
        self.download_calls += 1
        if self._data is None:
            raise FileNotFoundError("404 blob not found")
        return self._data


class _FakeBucket:
    def __init__(self, blobs: dict[str, _FakeBlob]) -> None:
        self._blobs = blobs

    def blob(self, path: str) -> _FakeBlob:
        return self._blobs.get(path, _FakeBlob(None))


class _FakeStorageClient:
    def __init__(self, buckets: dict[str, dict[str, _FakeBlob]]) -> None:
        self._buckets = {name: _FakeBucket(b) for name, b in buckets.items()}
        self.requested_buckets: list[str] = []

    def bucket(self, name: str) -> _FakeBucket:
        self.requested_buckets.append(name)
        if name not in self._buckets:
            raise AssertionError(f"Unexpected GCS bucket access: {name!r}")
        return self._buckets[name]


def test_delivery_kit_zip_structure_csv_defense_and_bucket_confinement() -> None:
    async def _run() -> None:
        session_svc = InMemorySessionService()
        artifact_svc = InMemoryArtifactService()

        state = _sample_session_state(2)
        # Inject formula-injection payloads into copy 1 to verify OWASP CSV neutralization.
        state["ad_copy_critique"]["ad_copies"][0]["headline"] = "=CMD|' /C calc'!A0"
        state["ad_copy_critique"]["ad_copies"][0]["call_to_action"] = "@SUM(1+1)"
        state["ad_copy_critique"]["ad_copies"][0]["social_caption"] = (
            '  +HYPERLINK("x")'
        )

        # Mark concept 2 as a controlled variation to verify manifest variation_lineage.
        state["final_visual_concepts"]["visual_concepts"][1].update(
            {
                "variation_of": "Stadium Encore 1",
                "variation_base_index": 0,
                "variation_axis": "visual_style",
                "variation_value": "Comic panel",
            }
        )

        # Include 1 confined video + 3 hostile/unconfined URIs (cross-bucket, path traversal, https SSRF).
        state["generated_videos"] = {
            "Hero Cut 15s": f"gs://{ALLOWED_BUCKET}/out/run1/hero_cut.mp4",
            "Cross Bucket": "gs://evil-exfil-bucket/secrets.mp4",
            "Traversal Cut": f"gs://{ALLOWED_BUCKET}/out/../escape.mp4",
            "SSRF Cut": "https://169.254.169.254/latest/meta-data/",
        }

        await session_svc.create_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-kit-99",
            state=state,
        )

        # Save concept 1 PNG in ADK artifact_service; let concept 2 PNG, PDF, HTML, and MP4 load from confined GCS.
        c1_key = artifact_key_for(
            state["final_visual_concepts"]["visual_concepts"][0]["concept_name"]
        )
        c2_key = artifact_key_for(
            state["final_visual_concepts"]["visual_concepts"][1]["concept_name"]
        )
        await artifact_svc.save_artifact(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-kit-99",
            filename=c1_key,
            artifact=types.Part.from_bytes(data=PNG_BYTES, mime_type="image/png"),
        )

        fake_gcs = _FakeStorageClient(
            {
                ALLOWED_BUCKET: {
                    f"out/run1/{c2_key}": _FakeBlob(PNG_BYTES),
                    "out/run1/research_report_with_citations.pdf": _FakeBlob(
                        b"%PDF-1.4 fake research pdf"
                    ),
                    "out/run1/creative_portfolio_gallery.html": _FakeBlob(
                        b"<!doctype html><html><body>Gallery</body></html>"
                    ),
                    "out/run1/hero_cut.mp4": _FakeBlob(
                        b"\x00\x00\x00\x18ftypmp42video"
                    ),
                }
            }
        )

        app = _build_test_app(
            session_service=session_svc,
            artifact_service=artifact_svc,
            gcs_bucket=ALLOWED_BUCKET,
            gcs_client_factory=lambda: fake_gcs,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get(
                f"/runs/creative_agent/{ALICE}/sess-kit-99/delivery-kit"
            )
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "application/zip"
            assert (
                'attachment; filename="trend-trawler-prs-guitars-sess-kit-99.zip"'
                == resp.headers["content-disposition"]
            )

            # Cross-bucket was NEVER requested on the storage client!
            assert set(fake_gcs.requested_buckets) == {ALLOWED_BUCKET}

            with zipfile.ZipFile(io.BytesIO(resp.content), mode="r") as zf:
                names = set(zf.namelist())
                expected = {
                    "manifest.json",
                    "brief/creative_brief.json",
                    "brief/creative_brief.md",
                    "research/research_report.md",
                    "research/research_report.pdf",
                    "copy/ad_copies.json",
                    "copy/ad_copies.csv",
                    "visuals/stadium-encore-1.png",
                    "visuals/stadium-encore-1.json",
                    "visuals/stadium-encore-2.png",
                    "visuals/stadium-encore-2.json",
                    "videos/hero-cut-15s.mp4",
                    "evaluation/creative_evaluation_report.json",
                    "gallery/index.html",
                }
                assert expected.issubset(names)

                # Verify CSV formula-injection defense.
                csv_text = zf.read("copy/ad_copies.csv").decode("utf-8")
                rows = list(csv.DictReader(io.StringIO(csv_text)))
                assert rows[0]["headline"].startswith("'=CMD")
                assert rows[0]["call_to_action"].startswith("'@SUM")
                assert rows[0]["social_caption"].startswith("'  +HYPERLINK")
                assert rows[0]["original_id"] == "1"

                # Verify manifest skipped_files recorded all 3 confinement blocks accurately.
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                assert manifest["counts"]["variations"] == 1
                reasons = {
                    item["archive_path"]: item["reason"]
                    for item in manifest["skipped_files"]
                }
                assert reasons["videos/cross-bucket.mp4"] == "cross_bucket_blocked"
                assert reasons["videos/traversal-cut.mp4"] == "invalid_blob_path"
                assert reasons["videos/ssrf-cut.mp4"] == "non_gcs_scheme"

    asyncio.run(_run())


def test_delivery_kit_enforces_150mb_cap_before_and_during_archive_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        session_svc = InMemorySessionService()
        artifact_svc = InMemoryArtifactService()

        # Case A: Oversized blob.size > MAX_ARCHIVE_BYTES rejected with 413 BEFORE download_as_bytes()!
        oversized_blob = _FakeBlob(
            b"small-actual-bytes",
            reported_size=delivery_kit.MAX_ARCHIVE_BYTES + 1,
        )
        state_big_blob = deepcopy(_sample_session_state(1))
        state_big_blob["generated_videos"] = {
            "Huge Raw Video": f"gs://{ALLOWED_BUCKET}/out/run1/huge.mp4"
        }
        await session_svc.create_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-413-blob",
            state=state_big_blob,
        )
        fake_gcs = _FakeStorageClient(
            {
                ALLOWED_BUCKET: {
                    "out/run1/huge.mp4": oversized_blob,
                }
            }
        )
        app = _build_test_app(
            session_service=session_svc,
            artifact_service=artifact_svc,
            gcs_bucket=ALLOWED_BUCKET,
            gcs_client_factory=lambda: fake_gcs,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r_big = await client.get(
                f"/runs/creative_agent/{ALICE}/sess-413-blob/delivery-kit"
            )
            assert r_big.status_code == 413
            assert oversized_blob.download_calls == 0

            # Case B: Cumulative multi-file payload exceeding monkeypatched MAX_ARCHIVE_BYTES -> 413.
            monkeypatch.setattr(delivery_kit, "MAX_ARCHIVE_BYTES", 512)
            await session_svc.create_session(
                app_name="creative_agent",
                user_id=ALICE,
                session_id="sess-413-cum",
                state=_sample_session_state(2),
            )
            r_cum = await client.get(
                f"/runs/creative_agent/{ALICE}/sess-413-cum/delivery-kit"
            )
            assert r_cum.status_code == 413

    asyncio.run(_run())


def test_delivery_kit_authz_and_minimal_empty_session() -> None:
    async def _run() -> None:
        session_svc = InMemorySessionService()
        artifact_svc = InMemoryArtifactService()
        await session_svc.create_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-empty",
            state={"brand": 'Weird "Brand"\r\nName'},
        )

        app = _build_test_app(
            session_service=session_svc,
            artifact_service=artifact_svc,
            authz_mode=AuthzMode.ENFORCE,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r_401 = await client.get(
                f"/runs/creative_agent/{ALICE}/sess-empty/delivery-kit"
            )
            assert r_401.status_code == 401

            r_403 = await client.get(
                f"/runs/creative_agent/{ALICE}/sess-empty/delivery-kit",
                headers={"authorization": "Bearer proxy", "x-tt-user": BOB},
            )
            assert r_403.status_code == 403

            r_200 = await client.get(
                f"/runs/creative_agent/{ALICE}/sess-empty/delivery-kit",
                headers={"authorization": "Bearer proxy", "x-tt-user": ALICE},
            )
            assert r_200.status_code == 200
            assert (
                r_200.headers["content-disposition"]
                == 'attachment; filename="trend-trawler-weird-brand-name-sess-empty.zip"'
            )
            with zipfile.ZipFile(io.BytesIO(r_200.content), mode="r") as zf:
                assert zf.namelist() == ["manifest.json"]

    asyncio.run(_run())


def test_ratings_cache_invalidation_and_state_fallback_after_variations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from creative_agent.config import config as ca_config

    monkeypatch.setattr(ca_config, "GCS_BUCKET_NAME", "")

    async def _run() -> None:
        session_svc = InMemorySessionService()
        artifact_svc = InMemoryArtifactService()
        ratings_store = InMemoryRatingsStore()

        eval_uri = f"gs://{ALLOWED_BUCKET}/out/run1/creative_eval_report.json"
        init_state = _sample_session_state(4)
        init_state["eval_report_gcs_uri"] = eval_uri

        # Simulate GCS blob storage for creative_eval_report.json.
        gcs_reports: dict[str, dict[str, Any]] = {
            eval_uri: deepcopy(init_state["creative_evaluation_report"])
        }
        loader_calls: list[str] = []

        def _fake_report_loader(uri: str) -> Any:
            loader_calls.append(uri)
            return deepcopy(gcs_reports[uri])

        await session_svc.create_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-rate-1",
            state=init_state,
        )

        app = _build_test_app(
            session_service=session_svc,
            artifact_service=artifact_svc,
        )
        ratings.configure(
            session_service=session_svc,
            store=ratings_store,
            report_loader=_fake_report_loader,
            report_bucket=ALLOWED_BUCKET,
        )
        app.include_router(ratings.router)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # 1. Rate a base creative -> warms ratings._REPORT_CACHE from GCS.
            r_base = await client.put(
                f"/ratings/{ALICE}/sess-rate-1",
                json={
                    "app_name": "creative_agent",
                    "creative_key": "visual:Stadium Encore 1",
                    "kind": "visual",
                    "verdict": "pass",
                    "score": 5,
                },
            )
            assert r_base.status_code == 200, r_base.text
            assert r_base.json()["judge_source"] == "gcs"
            assert r_base.json()["judge_overall"] == 0.9
            assert r_base.json()["judge_passed"] is True
            assert len(loader_calls) == 1
            assert eval_uri in ratings._REPORT_CACHE

            # 2. Generate 1 variation AND simulate GCS report refresh succeeding:
            #    ratings._REPORT_CACHE must be invalidated so the updated GCS report is loaded!
            r_var1 = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-rate-1/variations",
                json={
                    "base_concept_index": 0,
                    "axis": "visual_style",
                    "count": 1,
                    "target_values": ["Comic panel"],
                },
            )
            assert r_var1.status_code == 200, r_var1.text
            assert eval_uri not in ratings._REPORT_CACHE
            var1_name = r_var1.json()["variations"][0]["concept_name"]
            gcs_reports[eval_uri] = deepcopy(
                r_var1.json()["creative_evaluation_report"]
            )

            r_rate_var1 = await client.put(
                f"/ratings/{ALICE}/sess-rate-1",
                json={
                    "app_name": "creative_agent",
                    "creative_key": f"visual:{var1_name}",
                    "kind": "visual",
                    "verdict": "pass",
                    "score": 4,
                },
            )
            assert r_rate_var1.status_code == 200, r_rate_var1.text
            assert r_rate_var1.json()["judge_source"] == "gcs"
            assert r_rate_var1.json()["judge_overall"] == 0.8
            assert r_rate_var1.json()["judge_passed"] is True
            assert len(loader_calls) == 2

            # 3. Generate another variation where GCS report refresh failed soft
            #    (gcs_reports[eval_uri] remains stale without var2):
            #    rating var2 must fall back to state["creative_evaluation_report"]!
            r_var2 = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-rate-1/variations",
                json={
                    "base_concept_index": 1,
                    "axis": "composition_mode",
                    "count": 1,
                    "target_values": ["product_hero"],
                },
            )
            assert r_var2.status_code == 200, r_var2.text
            var2_name = r_var2.json()["variations"][0]["concept_name"]

            r_rate_var2 = await client.put(
                f"/ratings/{ALICE}/sess-rate-1",
                json={
                    "app_name": "creative_agent",
                    "creative_key": f"visual:{var2_name}",
                    "kind": "visual",
                    "verdict": "pass",
                    "score": 5,
                },
            )
            assert r_rate_var2.status_code == 200, r_rate_var2.text
            assert r_rate_var2.json()["judge_source"] == "state"
            assert r_rate_var2.json()["judge_overall"] == 0.8
            assert r_rate_var2.json()["judge_passed"] is True

    asyncio.run(_run())


def test_delivery_kit_preload_adk_artifacts_enforces_cumulative_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        session_svc = InMemorySessionService()
        artifact_svc = InMemoryArtifactService()

        # Cap at 1,000 bytes; save 3 ADK artifacts of 450 bytes each (450 < 1000, sum 1350 > 1000).
        monkeypatch.setattr(delivery_kit, "MAX_ARCHIVE_BYTES", 1000)
        state = _sample_session_state(3)
        await session_svc.create_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-preload-cap",
            state=state,
        )
        for vc in state["final_visual_concepts"]["visual_concepts"]:
            akey = artifact_key_for(vc["concept_name"])
            await artifact_svc.save_artifact(
                app_name="creative_agent",
                user_id=ALICE,
                session_id="sess-preload-cap",
                filename=akey,
                artifact=types.Part.from_bytes(data=b"x" * 450, mime_type="image/png"),
            )

        app = _build_test_app(
            session_service=session_svc,
            artifact_service=artifact_svc,
        )

        # Direct unit check that _preload_adk_artifacts itself raises before _build_zip_sync.
        preload_budget = delivery_kit._ArchiveBudget(max_bytes=1000)
        with pytest.raises(delivery_kit._ArchiveSizeLimitExceeded):
            await delivery_kit._preload_adk_artifacts(
                app_name="creative_agent",
                user_id=ALICE,
                session_id="sess-preload-cap",
                filenames={
                    artifact_key_for(vc["concept_name"])
                    for vc in state["final_visual_concepts"]["visual_concepts"]
                },
                budget=preload_budget,
            )

        # Endpoint check -> 413 Payload Too Large.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get(
                f"/runs/creative_agent/{ALICE}/sess-preload-cap/delivery-kit"
            )
            assert resp.status_code == 413

    asyncio.run(_run())


def test_variations_upstream_error_mapping_and_all_renders_failed_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from creative_agent import gcs_tools, tools
    from creative_agent.config import config as ca_config

    async def _run() -> None:
        session_svc = InMemorySessionService()
        artifact_svc = InMemoryArtifactService()
        await session_svc.create_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-err-1",
            state=_sample_session_state(4),
        )

        app = _build_test_app(
            session_service=session_svc,
            artifact_service=artifact_svc,
        )
        transport = httpx.ASGITransport(app=app)

        # 1. TimeoutError -> 504
        async def _timeout_runner(**kwargs: Any) -> Any:
            raise TimeoutError("Gemini worker timed out")

        variations.configure(
            session_service=session_svc,
            artifact_service=artifact_svc,
            variation_runner=_timeout_runner,
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r_504 = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-err-1/variations",
                json={"base_concept_index": 0, "axis": "visual_style", "count": 1},
            )
            assert r_504.status_code == 504
            assert "timed out" in r_504.json()["detail"].lower()

        # 2. Upstream 429 RESOURCE_EXHAUSTED -> 429
        async def _rate_limit_runner(**kwargs: Any) -> Any:
            raise genai_errors.ClientError(
                429,
                {
                    "error": {
                        "code": 429,
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "Quota exceeded",
                    }
                },
            )

        variations.configure(
            session_service=session_svc,
            artifact_service=artifact_svc,
            variation_runner=_rate_limit_runner,
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r_429 = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-err-1/variations",
                json={"base_concept_index": 0, "axis": "visual_style", "count": 1},
            )
            assert r_429.status_code == 429
            assert "rate-limited" in r_429.json()["detail"].lower()

        # 3. All variation renders returning None -> RuntimeError -> 502, zero unrendered cards saved!
        monkeypatch.setattr(ca_config, "GCS_BUCKET_NAME", "")

        async def _null_render(contents: Any, aspect_ratio: str) -> None:
            return None

        async def _all_null_renders_runner(**kwargs: Any) -> Any:
            return await generate_variations_for_session(
                **kwargs,
                draft_fn=lambda **kw: [
                    {"target_value": "Film", "visual_style": "Film"}
                ],
                render_fn=_null_render,
                eval_copy_fn=lambda c, ctx: _make_ad_eval(1, "H"),
                eval_visual_fn=lambda c, ctx, img: _make_vis_eval(1, c["concept_name"]),
            )

        variations.configure(
            session_service=session_svc,
            artifact_service=artifact_svc,
            variation_runner=_all_null_renders_runner,
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r_502 = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-err-1/variations",
                json={"base_concept_index": 0, "axis": "visual_style", "count": 1},
            )
            assert r_502.status_code == 502
            assert "failed upstream" in r_502.json()["detail"].lower()

        untouched = await session_svc.get_session(
            app_name="creative_agent", user_id=ALICE, session_id="sess-err-1"
        )
        assert untouched is not None
        assert len(untouched.state["final_visual_concepts"]["visual_concepts"]) == 4

        # 4. GCS configured: fail-soft image upload omits _generated_artifact_keys while
        #    minted eval_report_gcs_uri / creative_gallery_gcs_uri propagate to state.
        monkeypatch.setattr(ca_config, "GCS_BUCKET_NAME", ALLOWED_BUCKET)

        def _fail_image_gcs(**kwargs: Any) -> str:
            raise OSError("transient image upload failure")

        def _mint_eval_uri(tool_ctx: Any) -> str:
            uri = f"gs://{ALLOWED_BUCKET}/out/run1/creative_eval_report.json"
            tool_ctx.state["eval_report_gcs_uri"] = uri
            return uri

        async def _mint_gallery_uri(tool_ctx: Any) -> dict[str, Any]:
            uri = f"gs://{ALLOWED_BUCKET}/out/run1/creative_portfolio_gallery.html"
            return {"status": "success", "gcs_uri": uri}

        monkeypatch.setattr(gcs_tools, "_save_to_gcs", _fail_image_gcs)
        monkeypatch.setattr(gcs_tools, "save_eval_report_to_gcs", _mint_eval_uri)
        monkeypatch.setattr(tools, "save_creative_gallery_html", _mint_gallery_uri)

        async def _ok_render(contents: Any, aspect_ratio: str) -> tuple[bytes, str]:
            return PNG_BYTES, "image/png"

        async def _gcs_test_runner(**kwargs: Any) -> Any:
            return await generate_variations_for_session(
                **kwargs,
                draft_fn=lambda **kw: [
                    {"target_value": "Film", "visual_style": "Film"}
                ],
                render_fn=_ok_render,
                eval_copy_fn=lambda c, ctx: _make_ad_eval(1, "H"),
                eval_visual_fn=lambda c, ctx, img: _make_vis_eval(1, c["concept_name"]),
            )

        state_no_eval_uri = _sample_session_state(4)
        state_no_eval_uri.pop("creative_gallery_gcs_uri", None)
        await session_svc.create_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-gcs-prop",
            state=state_no_eval_uri,
        )
        variations.configure(
            session_service=session_svc,
            artifact_service=artifact_svc,
            variation_runner=_gcs_test_runner,
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r_prop = await client.post(
                f"/runs/creative_agent/{ALICE}/sess-gcs-prop/variations",
                json={"base_concept_index": 0, "axis": "visual_style", "count": 1},
            )
            assert r_prop.status_code == 200, r_prop.text
            minted_var_name = r_prop.json()["variations"][0]["concept_name"]
            minted_akey = artifact_key_for(minted_var_name)

        updated_prop = await session_svc.get_session(
            app_name="creative_agent", user_id=ALICE, session_id="sess-gcs-prop"
        )
        assert updated_prop is not None
        # Image upload failed soft -> artifact_key is NOT added to _generated_artifact_keys
        assert minted_akey not in updated_prop.state["_generated_artifact_keys"]
        # Minted GCS URIs from _refresh_gcs_deliverables_fail_soft propagated to state!
        assert (
            updated_prop.state["eval_report_gcs_uri"]
            == f"gs://{ALLOWED_BUCKET}/out/run1/creative_eval_report.json"
        )
        assert (
            updated_prop.state["creative_gallery_gcs_uri"]
            == f"gs://{ALLOWED_BUCKET}/out/run1/creative_portfolio_gallery.html"
        )

    asyncio.run(_run())


def test_variations_real_render_path_uses_render_concept_references_and_rerender_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from creative_agent import image_tools, render_concept
    from creative_agent.config import config as ca_config

    monkeypatch.setattr(ca_config, "GCS_BUCKET_NAME", "")
    monkeypatch.setattr(ca_config, "image_qa_enabled", True)

    render_prompts: list[Any] = []

    async def _fake_render_image(
        contents: Any, aspect_ratio: str
    ) -> tuple[bytes, str] | None:
        render_prompts.append(contents)
        return PNG_BYTES, "image/png"

    async def _fake_fetch_references(
        refs: list[tuple[str, str]],
    ) -> render_concept.RenderReferences:
        part = types.Part.from_bytes(data=PNG_BYTES, mime_type="image/png")
        return render_concept.RenderReferences(
            parts=(("logo", part),),
            missing_roles=(),
        )

    rerender_calls: list[render_concept.QAContext] = []

    async def _fake_rerender_loop(
        qa_ctx: render_concept.QAContext,
        rerender_fn: Any,
        budget: Any,
        *,
        entry: dict[str, Any],
        first: tuple[bytes, str],
        **_: Any,
    ) -> tuple[tuple[bytes, str], int, dict[str, Any] | None, str | None]:
        rerender_calls.append(qa_ctx)
        second = await rerender_fn("Remove stray background badge.")
        assert second is not None
        return second, 2, {"passed": True, "failures": []}, None

    monkeypatch.setattr(image_tools, "_render_image", _fake_render_image)
    monkeypatch.setattr(render_concept, "fetch_references", _fake_fetch_references)
    monkeypatch.setattr(render_concept, "_rerender_loop", _fake_rerender_loop)

    async def _run() -> None:
        state = _sample_session_state(2)
        state["reference_images"] = [
            {"uri": f"gs://{ALLOWED_BUCKET}/refs/logo.png", "role": "logo"}
        ]
        state["rating_strictness"] = ["product_not_visible"]
        res = await generate_variations_for_session(
            app_name="creative_agent",
            user_id=ALICE,
            session_id="sess-qa-loop",
            state=state,
            base_concept_index=0,
            axis="visual_style",
            count=1,
            target_values=["Comic panel"],
            draft_fn=lambda **kw: [
                {
                    "target_value": "Comic panel",
                    "visual_style": "Comic panel",
                    "image_generation_prompt": "Comic panel of PRS Custom 24 on stage.",
                }
            ],
            render_fn=None,
            eval_copy_fn=lambda c, ctx: _make_ad_eval(1, "H"),
            eval_visual_fn=lambda c, ctx, img: _make_vis_eval(1, c["concept_name"]),
        )
        assert len(rerender_calls) == 1
        assert rerender_calls[0].has_logo is True
        assert "product_not_visible" in rerender_calls[0].strictness
        # First render + 1 QA re-render = 2 image model calls, both carrying reference Part + no_other_logos_line
        assert len(render_prompts) == 2
        assert isinstance(render_prompts[0], list)
        assert (
            "third-party" in render_prompts[0][0].lower()
            or "logo" in render_prompts[0][0].lower()
        )
        cname = res.new_concepts[0]["concept_name"]
        assert res.new_generated_images[cname]["attempts"] == 2

    asyncio.run(_run())
