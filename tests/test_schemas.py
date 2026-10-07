"""Tests for Pydantic schemas in the creative_agent pipeline."""

import pytest
from pydantic import ValidationError


def test_ad_copy_schema_valid():
    from creative_agent.agent import AdCopy

    data = AdCopy(
        id=1,
        tone_style="Humorous",
        headline="Get Your Groove On",
        body_text="This guitar will change your life. Seriously.",
        trend_connection="Leverages the Taylor Swift engagement buzz.",
        audience_appeal_rationale="Musicians love pop culture references.",
        social_caption="Your next riff starts here #PRS",
    )
    assert data.id == 1
    assert data.tone_style == "Humorous"


def test_ad_copy_invalid_tone():
    from creative_agent.agent import AdCopy

    with pytest.raises(ValidationError):
        AdCopy(
            id=1,
            tone_style="InvalidTone",
            headline="Test",
            body_text="Test body",
            trend_connection="Test",
            audience_appeal_rationale="Test",
            social_caption="Test",
        )


def test_ad_copy_missing_required_field():
    from creative_agent.agent import AdCopy

    with pytest.raises(ValidationError):
        AdCopy(
            id=1,
            tone_style="Humorous",
            # missing headline
            body_text="Test body",
            trend_connection="Test",
            audience_appeal_rationale="Test",
            social_caption="Test",
        )


def test_final_ad_copy_schema():
    from creative_agent.agent import FinalAdCopy

    data = FinalAdCopy(
        original_id=3,
        tone_style="Aspirational",
        headline="Dream Big",
        body_text="Premium tone meets premium craft.",
        trend_connection="Connects to trending aspirational content.",
        audience_appeal_rationale="Appeals to ambitious musicians.",
        social_caption="Level up your sound",
        call_to_action="Shop now!",
        detailed_performance_rationale="Strong alignment with audience values and trend momentum.",
    )
    assert data.original_id == 3
    assert data.call_to_action == "Shop now!"


def test_ad_copy_list_schema():
    from creative_agent.agent import AdCopy, AdCopyList

    copies = [
        AdCopy(
            id=i,
            tone_style="Humorous",
            headline=f"Headline {i}",
            body_text=f"Body {i}",
            trend_connection=f"Connection {i}",
            audience_appeal_rationale=f"Appeal {i}",
            social_caption=f"Caption {i}",
        )
        for i in range(1, 4)
    ]
    lst = AdCopyList(ad_copies=copies)
    assert len(lst.ad_copies) == 3


def test_ad_copy_list_allows_none():
    from creative_agent.agent import AdCopyList

    lst = AdCopyList(ad_copies=None)
    assert lst.ad_copies is None


def test_visual_concept_schema():
    from creative_agent.agent import VisualConcept

    vc = VisualConcept(
        ad_copy_id=1,
        concept_name="Sunset Serenade",
        trend_visual_link="Shows trending sunset aesthetic.",
        concept_summary="A guitarist silhouetted against a vibrant sunset.",
        image_generation_prompt="Photorealistic 9:16 portrait of a guitarist...",
    )
    assert vc.concept_name == "Sunset Serenade"
    # visual_style / aspect_ratio are additive + defaulted (construction without
    # them must keep working — see invariant #3 in the overhaul plan).
    assert vc.visual_style == ""
    assert vc.aspect_ratio == ""

    styled = VisualConcept(
        ad_copy_id=2,
        concept_name="Meme Drop",
        trend_visual_link="Riffs on the trending meme format.",
        concept_summary="A bold caption meme of the product.",
        image_generation_prompt="A meme-style image with top caption '...'.",
        visual_style="meme aesthetic",
        aspect_ratio="9:16",
    )
    assert styled.visual_style == "meme aesthetic"
    assert styled.aspect_ratio == "9:16"


def test_visual_concept_final_schema():
    from creative_agent.agent import VisualConceptFinal

    vcf = VisualConceptFinal(
        ad_copy_id=1,
        concept_name="Final Concept",
        trend="Taylor Swift",
        trend_reference="References the engagement buzz.",
        markets_product="Showcases PRS guitar design.",
        audience_appeal="Resonates with musician lifestyle.",
        selection_rationale="Best commercial viability.",
        headline="Rock Your World",
        social_caption="New vibes only",
        call_to_action="Get yours now",
        concept_summary="A polished visual of the guitar in a trending setting.",
        image_generation_prompt="Ultra HD 9:16 photorealistic...",
    )
    assert vcf.trend == "Taylor Swift"
    assert vcf.headline == "Rock Your World"


def test_visual_concept_final_missing_headline():
    from creative_agent.agent import VisualConceptFinal

    with pytest.raises(ValidationError):
        VisualConceptFinal(
            ad_copy_id=1,
            concept_name="Test",
            trend="Test",
            trend_reference="Test",
            markets_product="Test",
            audience_appeal="Test",
            selection_rationale="Test",
            # missing headline
            social_caption="Test",
            call_to_action="Test",
            concept_summary="Test",
            image_generation_prompt="Test",
        )


def test_research_feedback_schema():
    from creative_agent.agent import ResearchFeedback, SearchQuery

    fb = ResearchFeedback(
        finding_type="Gap",
        analysis_comment="Missing audience sentiment data.",
        follow_up_queries=[
            SearchQuery(search_query="PRS guitars audience sentiment 2024"),
            SearchQuery(search_query="musician purchase behavior trends"),
        ],
    )
    assert fb.finding_type == "Gap"
    assert len(fb.follow_up_queries) == 2


def test_research_feedback_invalid_finding_type():
    from creative_agent.agent import ResearchFeedback

    with pytest.raises(ValidationError):
        ResearchFeedback(
            finding_type="Invalid",
            analysis_comment="Test",
        )


def test_tone_style_enum_values():
    from creative_agent.agent import AdCopy

    valid_tones = [
        "Humorous",
        "Aspirational",
        "Problem/Solution",
        "Emotional/Authentic",
        "Educational/Informative",
        "Relatable/Meme-based",
    ]
    for tone in valid_tones:
        copy = AdCopy(
            id=1,
            tone_style=tone,
            headline="Test",
            body_text="Test",
            trend_connection="Test",
            audience_appeal_rationale="Test",
            social_caption="Test",
        )
        assert copy.tone_style == tone


# --- CREATIVE BRIEF SCHEMA -------------------------------------------------


def _brief_payload(**overrides):
    data = {
        "objective": "Drive trial of Rocket Skates among coyotes this week.",
        "audience": "Coyotes who chase roadrunners for sport.",
        "insight": "Coyotes want to win the chase, but every gadget backfires.",
        "single_minded_proposition": "Rocket Skates finally make you faster.",
        "reasons_to_believe": [
            {"claim": "Top speed 90 mph", "source_id": "brief"},
            {"claim": "Roadrunner sightings up 40%", "source_id": "src-1"},
        ],
        "brand": {
            "tone_of_voice": "Deadpan, slapstick confidence",
            "distinctive_assets": ["the ACME crate"],
            "do_not": ["mock the customer"],
        },
        "trend_bridge": {
            "fit_score": 4,
            "fit_mode": "direct",
            "bridge": "Skate speed connects to the roadrunner's signature sprint.",
            "motifs": ["a roadrunner dust cloud"],
            "risks": ["anvil jokes feel dated"],
        },
        "mandatories": ["show the ACME logo"],
        "avoid": ["cliff falls"],
        "desired_response": "Think: speed is possible. Feel: hopeful. Do: order.",
        "angles": [
            {"angle_id": f"A{i}", "name": f"Angle {i}", "tension": "t", "route": "r"}
            for i in range(1, 4)
        ],
    }
    data.update(overrides)
    return data


def test_creative_brief_valid_and_reexported():
    from creative_agent.agent import CreativeBrief

    brief = CreativeBrief(**_brief_payload())
    assert brief.trend_bridge.fit_mode == "direct"
    assert brief.reasons_to_believe[1].source_id == "src-1"
    assert len(brief.angles) == 3


@pytest.mark.parametrize("score", [0, 6])
def test_trend_bridge_fit_score_bounds(score):
    from creative_agent.schemas import TrendBridge

    with pytest.raises(ValidationError):
        TrendBridge(
            fit_score=score, fit_mode="direct", bridge="b", motifs=["m"], risks=[]
        )


def test_trend_bridge_fit_mode_literal():
    from creative_agent.schemas import TrendBridge

    with pytest.raises(ValidationError):
        TrendBridge(fit_score=3, fit_mode="forced", bridge="b", motifs=[], risks=[])
    assert (
        TrendBridge(
            fit_score=2, fit_mode="light_touch", bridge="b", motifs=[], risks=[]
        ).fit_mode
        == "light_touch"
    )


@pytest.mark.parametrize("count", [2, 6])
def test_creative_brief_angle_count_bounds(count):
    from creative_agent.schemas import CreativeBrief

    angles = [
        {"angle_id": f"A{i}", "name": f"n{i}", "tension": "t", "route": "r"}
        for i in range(count)
    ]
    with pytest.raises(ValidationError):
        CreativeBrief(**_brief_payload(angles=angles))


def test_reason_to_believe_source_id_optional():
    from creative_agent.schemas import ReasonToBelieve

    assert ReasonToBelieve(claim="c", source_id=None).source_id is None


def test_creative_brief_constraints_reach_the_model_schema():
    """The bounds are real model-facing constraints: google-genai maps them to
    Vertex Schema minimum/maximum and min_items/max_items, so the model is
    steered by them (and ADK's output_schema validation re-draws violations
    via SCHEMA_RETRY)."""
    from creative_agent.schemas import CreativeBrief

    schema = CreativeBrief.model_json_schema()
    assert schema["properties"]["angles"]["minItems"] == 3
    assert schema["properties"]["angles"]["maxItems"] == 5
    bridge = schema["$defs"]["TrendBridge"]["properties"]
    assert bridge["fit_score"]["minimum"] == 1
    assert bridge["fit_score"]["maximum"] == 5
    assert bridge["fit_mode"]["enum"] == ["direct", "cultural", "light_touch"]


# --- Ad copy angles + typicality (diversity, research F5) -------------------


def _ad_copy_payload(**overrides):
    data = {
        "id": 1,
        "tone_style": "Humorous",
        "headline": "h",
        "body_text": "b",
        "trend_connection": "t",
        "audience_appeal_rationale": "a",
        "social_caption": "c",
    }
    data.update(overrides)
    return data


def _final_ad_copy_payload(**overrides):
    data = _ad_copy_payload()
    data["original_id"] = data.pop("id")
    data.update(call_to_action="Shop now", detailed_performance_rationale="r")
    data.update(overrides)
    return data


def test_ad_copy_angle_and_typicality_default_for_old_payloads():
    """Old sessions (no angle_id/typicality) still load."""
    from creative_agent.schemas import AdCopy, FinalAdCopy

    for model, payload in (
        (AdCopy, _ad_copy_payload()),
        (FinalAdCopy, _final_ad_copy_payload()),
    ):
        copy = model(**payload)
        assert copy.angle_id == ""
        assert copy.typicality is None


def test_ad_copy_angle_and_typicality_round_trip():
    from creative_agent.schemas import AdCopy, FinalAdCopy

    copy = AdCopy(**_ad_copy_payload(angle_id="A2", typicality=0.3))
    assert (copy.angle_id, copy.typicality) == ("A2", 0.3)
    final = FinalAdCopy(**_final_ad_copy_payload(angle_id="A1", typicality=1.0))
    assert (final.angle_id, final.typicality) == ("A1", 1.0)


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_ad_copy_typicality_bounds(value):
    from creative_agent.schemas import AdCopy, FinalAdCopy

    with pytest.raises(ValidationError):
        AdCopy(**_ad_copy_payload(typicality=value))
    with pytest.raises(ValidationError):
        FinalAdCopy(**_final_ad_copy_payload(typicality=value))


def test_ad_copy_typicality_bounds_reach_the_model_schema():
    from creative_agent.schemas import AdCopy

    prop = AdCopy.model_json_schema()["properties"]["typicality"]
    number = next(s for s in prop["anyOf"] if s.get("type") == "number")
    assert (number["minimum"], number["maximum"]) == (0.0, 1.0)
    assert "unexpected" in prop["description"]
