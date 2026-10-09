"""runserver/share_snapshot.py: the allowlisted, frozen share snapshot (pure)."""

from __future__ import annotations

import json

import pytest

from runserver.share_snapshot import (
    CREATIVE_FIELDS,
    SnapshotError,
    build_snapshot,
    match_by_id_headline_index,
)
from tests._creative_fixtures import (
    CONCEPTS,
    creative_report,
    creative_state,
    image_uri,
)

NOW = "2026-10-09T00:00:00Z"


def _build(state=None, report=None, **kw):
    kw.setdefault("token", "tok")
    kw.setdefault("concept_names", None)
    kw.setdefault("include_eval", False)
    kw.setdefault("now", NOW)
    return build_snapshot(
        creative_state() if state is None else state,
        creative_report() if report is None else report,
        **kw,
    )


def test_slate_snapshot_allowlists_fields():
    built = _build()
    snap = built.snapshot
    assert snap["version"] == 1 and snap["token"] == "tok"
    assert snap["created_at"] == NOW and snap["scope"] == "slate"
    assert snap["include_eval"] is False
    assert snap["brand"] == "Paul Reed Smith (PRS)"
    assert snap["product"] == "SE CE24 Electric Guitar"
    assert snap["trend"] == "Powerball"
    assert len(snap["creatives"]) == 4
    c = snap["creatives"][0]
    assert (
        set(c)
        == set(CREATIVE_FIELDS)
        == {
            "index",
            "image",
            "aspect_ratio",
            "alt",
            "visual_style",
            "headline",
            "body",
            "caption",
            "cta",
            "tone",
        }
    )
    assert [x["index"] for x in snap["creatives"]] == [0, 1, 2, 3]
    assert [x["image"] for x in snap["creatives"]] == [f"{i}.png" for i in range(4)]
    assert c["aspect_ratio"] == "9:16"
    assert c["alt"].startswith("This concept brings the headline to life")
    assert c["visual_style"] == "Surreal Meme-Collage"
    assert c["headline"] == "If I Won The $1.8B Powerball, There To Be Signs."
    assert c["body"] and c["caption"] and c["cta"] and c["tone"]
    assert built.image_uris == [image_uri(i) for i in range(4)]
    assert built.concept_names == list(CONCEPTS)
    blob = json.dumps(snap)
    for secret in (
        "image_generation_prompt",
        "rationale",
        "selection_rationale",
        "trend_connection",
        "session",
        "gs://",
        "@",
    ):
        assert secret not in blob


def test_snapshot_is_json_serialisable_with_emoji():
    snap = _build().snapshot
    assert "🎸" in json.dumps(snap, ensure_ascii=False)


def test_copy_paired_by_id_then_headline_then_index():
    snap = _build().snapshot
    state = creative_state()
    copies = {a["headline"]: a for a in state["ad_copy_critique"]["ad_copies"]}
    for c in snap["creatives"]:
        assert c["body"] == copies[c["headline"]]["body_text"]
        assert c["tone"] == copies[c["headline"]]["tone_style"]


def test_match_by_id_headline_index_parity_with_frontend():
    items = [
        {"original_id": 1, "headline": "A"},
        {"original_id": 2, "headline": "B"},
        {"original_id": 3, "headline": "C"},
    ]
    # 1. id wins over headline
    assert (
        match_by_id_headline_index(items, {"ad_copy_id": 2, "headline": "C"}, 0)[
            "headline"
        ]
        == "B"
    )
    # 2. then headline
    assert (
        match_by_id_headline_index(items, {"ad_copy_id": 9, "headline": "C"}, 0)[
            "headline"
        ]
        == "C"
    )
    # 3. then index position
    assert (
        match_by_id_headline_index(items, {"ad_copy_id": 9, "headline": "Z"}, 1)[
            "headline"
        ]
        == "B"
    )
    assert (
        match_by_id_headline_index(items, {"ad_copy_id": 9, "headline": "Z"}, 5) is None
    )
    # float ids from JSON compare like JS numbers
    assert match_by_id_headline_index(items, {"ad_copy_id": 3.0}, 0)["headline"] == "C"


def test_concept_headline_used_when_no_copy():
    state = creative_state()
    state["ad_copy_critique"] = {"ad_copies": []}
    c = _build(state).snapshot["creatives"][0]
    assert c["headline"] == "If I Won The $1.8B Powerball, There To Be Signs."
    assert c["cta"] == "Claim Your Tonal Jackpot Now!"
    assert c["body"] == "" and c["tone"] == ""


def test_single_creative_and_eval():
    report = creative_report()
    score = report["visual_concept_evaluations"][2]["score"]
    score["gates"] = [
        {"gate": "product_visible", "passed": True, "note": "secret judge note"},
        {"gate": "brand_cue_present", "passed": False, "advisory": True},
    ]
    score["gates_passed"] = True
    built = _build(
        report=report, concept_names=["The Jackpot Reveal"], include_eval=True
    )
    snap = built.snapshot
    assert snap["scope"] == "creative" and snap["include_eval"] is True
    assert len(snap["creatives"]) == 1
    c = snap["creatives"][0]
    assert c["index"] == 0 and c["image"] == "0.png"
    assert built.image_uris == [image_uri(2)]
    assert built.concept_names == ["The Jackpot Reveal"]
    ev = c["eval"]
    assert ev["checks"] == [
        {
            "kind": "visual",
            "gate": "product_visible",
            "label": "Product visible",
            "passed": True,
            "advisory": False,
        },
        {
            "kind": "visual",
            "gate": "brand_cue_present",
            "label": "Brand cue present",
            "passed": False,
            "advisory": True,
        },
    ]
    assert ev["visual"] == {"passed": False, "score": 0.633}
    assert ev["copy"] == {"passed": True, "score": 0.75}
    assert ev["passed"] is False
    assert ev["score"] == pytest.approx((0.633 + 0.75) / 2, abs=1e-3)
    blob = json.dumps(snap)
    assert "secret judge note" not in blob and "rationale" not in blob


def test_eval_absent_unless_requested_and_null_without_report():
    assert "eval" not in _build().snapshot["creatives"][0]
    state = creative_state(with_report=False)
    snap = build_snapshot(
        state, None, token="t", concept_names=None, include_eval=True, now=NOW
    ).snapshot
    assert all(c["eval"] is None for c in snap["creatives"])


def test_subset_keeps_pipeline_order_and_is_a_slate():
    built = _build(concept_names=["The Authentic Encore", "The Golden Golf Cart Gig"])
    assert built.snapshot["scope"] == "slate"
    assert built.concept_names == ["The Golden Golf Cart Gig", "The Authentic Encore"]
    assert built.image_uris == [image_uri(0), image_uri(3)]


def test_unknown_concept_raises():
    with pytest.raises(SnapshotError) as exc:
        _build(concept_names=["nope"])
    assert exc.value.reason == "unknown_concept"
    assert isinstance(exc.value, ValueError)


def test_concepts_without_images_are_skipped():
    state = creative_state()
    del state["generated_images"]["Nihilistic Retirement Plan"]
    state["generated_images"]["The Jackpot Reveal"] = {"gcs_uri": ""}
    built = _build(state)
    assert built.concept_names == ["The Golden Golf Cart Gig", "The Authentic Encore"]
    assert [c["index"] for c in built.snapshot["creatives"]] == [0, 1]
    assert [c["image"] for c in built.snapshot["creatives"]] == ["0.png", "1.png"]


def test_no_images_raises():
    with pytest.raises(SnapshotError) as exc:
        _build(creative_state(with_images=False))
    assert exc.value.reason == "no_images"


def test_aspect_ratio_override_and_fallbacks():
    state = creative_state()
    state["visual_aspect_ratio"] = "1:1"
    assert {c["aspect_ratio"] for c in _build(state).snapshot["creatives"]} == {"1:1"}
    state = creative_state()
    state["visual_aspect_ratio"] = "bogus"
    concepts = state["final_visual_concepts"]["visual_concepts"]
    concepts[0]["aspect_ratio"] = "4:5"
    concepts[1]["aspect_ratio"] = "7:3"
    del concepts[2]["aspect_ratio"]
    ratios = [c["aspect_ratio"] for c in _build(state).snapshot["creatives"]]
    assert ratios[:3] == ["4:5", "9:16", "9:16"]


def test_json_string_state_and_text_coercion():
    state = creative_state()
    state["final_visual_concepts"] = json.dumps(state["final_visual_concepts"])
    state["target_search_trends"] = ["Powerball", "Lottery"]
    state["brand"] = "  PRS  "
    snap = _build(state).snapshot
    assert snap["trend"] == "Powerball, Lottery"
    assert snap["brand"] == "PRS"
    assert len(snap["creatives"]) == 4


def test_alt_falls_back_when_summary_missing():
    state = creative_state()
    state["final_visual_concepts"]["visual_concepts"][0]["concept_summary"] = ""
    c = _build(state).snapshot["creatives"][0]
    assert c["alt"]
