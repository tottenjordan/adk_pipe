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
    from creative_agent.agent import AdCopyList, AdCopy

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
