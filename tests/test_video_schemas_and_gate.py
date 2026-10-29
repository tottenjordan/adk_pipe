"""Tests for Phase 3 video schemas, storyboard gate, Checkpoint 4 resume edits, and deliverables."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from google.adk.sessions import InMemorySessionService
from google.adk.tools.long_running_tool import LongRunningFunctionTool
from pydantic import ValidationError

from agent_common.observability import collect_degradation_warnings
from creative_agent import (
    VideoStoryboard,
    callbacks,
    prompts,
    video_qa,
)
from creative_agent import agent as ca
from creative_agent.config import (
    ResearchConfiguration,
    parse_storyboard_revision_rounds,
    parse_video_gen_backend,
    parse_video_qa_max_retakes,
)
from creative_agent.finalize import finalize_summary
from creative_agent.schemas import VideoShotSpec
from creative_agent.tools import _build_video_reel_html
from interactive_creative.review_tools import (
    review_video_storyboard,
    review_video_storyboard_tool,
)
from runserver import async_runs, changelog
from runserver.authz import AuthzMode


def _valid_shots() -> list[dict[str, Any]]:
    """5 sequential shots summing to 30s hero, 14s social (>=2 shots), and 6s bumper (>=1 shot)."""
    return [
        {
            "shot_number": 1,
            "duration_seconds": 6,
            "anchor_concept_name": "Neon Sprint",
            "camera_motion": "Low-angle tracking dolly push",
            "visual_action_prompt": "Runner launches forward in Rocket Skates through neon mist.",
            "voiceover_line": "Built for city speed after midnight.",
            "on_screen_super": "Own Midnight",
            "audio_cue": "Crisp skate wheel whoosh",
            "include_in_15s": True,
            "include_in_6s": True,
        },
        {
            "shot_number": 2,
            "duration_seconds": 8,
            "anchor_concept_name": "Carbon Close-Up",
            "camera_motion": "Macro orbital pan",
            "visual_action_prompt": "Close-up of Rocket Skates carbon frame locking into place.",
            "voiceover_line": "Precision carbon chassis keeps every stride effortless.",
            "on_screen_super": "",
            "audio_cue": "Mechanical latch click",
            "include_in_15s": True,
            "include_in_6s": False,
        },
        {
            "shot_number": 3,
            "duration_seconds": 6,
            "anchor_concept_name": "Plaza Glide",
            "camera_motion": "Steadicam lateral follow",
            "visual_action_prompt": "Commuter glides across wet plaza tiles in Rocket Skates.",
            "voiceover_line": "Turn blocks into seconds on your commute.",
            "on_screen_super": "",
            "audio_cue": "Ambient night breeze",
            "include_in_15s": False,
            "include_in_6s": False,
        },
        {
            "shot_number": 4,
            "duration_seconds": 6,
            "anchor_concept_name": "Neon Sprint",
            "camera_motion": "Crane rise reveal",
            "visual_action_prompt": "Wide skyline reveal as Rocket Skates brake smoothly at crosswalk.",
            "voiceover_line": "Responsive braking whenever the light shifts.",
            "on_screen_super": "",
            "audio_cue": "Soft electronic swell",
            "include_in_15s": False,
            "include_in_6s": False,
        },
        {
            "shot_number": 5,
            "duration_seconds": 4,
            "anchor_concept_name": "Hero Lockup",
            "camera_motion": "Static hero pedestal hold",
            "visual_action_prompt": "Clean studio hero packshot of Rocket Skates under rim lighting.",
            "voiceover_line": "Step into Rocket Skates today.",
            "on_screen_super": "Acme — Ride Now",
            "audio_cue": "Signature chime resolve",
            "include_in_15s": False,
            "include_in_6s": False,
        },
    ]


def _valid_storyboard_dict(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Midnight Commute Commercial",
        "creative_direction": "Hook on midnight speed, prove carbon precision, resolve on hero lockup.",
        "music_direction": "Driving minimal synth pulse with warm sub-bass, instrumental only.",
        "voiceover_style": "Confident, crisp commercial narrator.",
        "shots": _valid_shots(),
    }
    payload.update(overrides)
    return payload


def _base_campaign_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "brand": "Acme",
        "target_product": "Rocket Skates",
        "target_search_trends": "night running club",
        "creative_brief": {
            "avoid": ["guaranteed miracles", "competitor swooshes"],
            "brand": {"do_not": ["slapstick falls"]},
            "mandatories": [],
        },
        "visual_avoid": "neon skulls, comic speech bubbles",
        "ad_copy_critique": {
            "ad_copies": [
                {
                    "original_id": 1,
                    "headline": "Own Midnight",
                    "call_to_action": "Ride Now",
                },
                {
                    "original_id": 2,
                    "headline": "Carbon Speed",
                    "call_to_action": "Shop Rocket Skates",
                },
            ]
        },
        "final_visual_concepts": {
            "visual_concepts": [
                {
                    "concept_name": "Neon Sprint",
                    "composition_mode": "cast_with_product",
                },
                {"concept_name": "Carbon Close-Up", "composition_mode": "product_hero"},
                {"concept_name": "Plaza Glide", "composition_mode": "product_in_scene"},
                {"concept_name": "Hero Lockup", "composition_mode": "graphic_lockup"},
            ]
        },
    }
    state.update(overrides)
    return state


def test_video_storyboard_pydantic_schema_validation():
    sb = VideoStoryboard.model_validate(_valid_storyboard_dict())
    assert len(sb.shots) == 5
    assert sum(s.duration_seconds for s in sb.shots) == 30
    assert sb.creative_direction.startswith("Hook on midnight speed")
    assert sb.music_direction.startswith("Driving minimal synth")
    assert sb.voiceover_style == "Confident, crisp commercial narrator."

    with pytest.raises(ValidationError):
        VideoShotSpec.model_validate({**_valid_shots()[0], "duration_seconds": 5})

    with pytest.raises(ValidationError):
        VideoStoryboard.model_validate(_valid_storyboard_dict(shots=_valid_shots()[:2]))

    with pytest.raises(ValidationError):
        VideoStoryboard.model_validate(
            _valid_storyboard_dict(shots=_valid_shots() + _valid_shots()[:2])
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "auto"),
        ("", "auto"),
        ("AUTO", "auto"),
        ("omniflash", "omniflash"),
        ("Veo", "veo"),
        ("unknown-backend", "auto"),
    ],
)
def test_video_gen_backend_parsing_and_env(monkeypatch, raw, expected):
    assert parse_video_gen_backend(raw) == expected
    if raw is None:
        monkeypatch.delenv("VIDEO_GEN_BACKEND", raising=False)
    else:
        monkeypatch.setenv("VIDEO_GEN_BACKEND", raw)
    assert ResearchConfiguration().video_gen_backend == expected


@pytest.mark.parametrize(
    ("fn", "env_key"),
    [
        (parse_video_qa_max_retakes, "VIDEO_QA_MAX_RETAKES"),
        (parse_storyboard_revision_rounds, "STORYBOARD_REVISION_ROUNDS"),
    ],
)
def test_video_clamped_int_knobs_and_deploy_forwarding(monkeypatch, fn, env_key):
    import deployment.deploy_agent as da

    assert fn(None) == 1
    assert fn("") == 1
    assert fn("0") == 0
    assert fn("2") == 2
    assert fn("9") == 2
    assert fn("-4") == 0
    assert fn("bad") == 1
    assert env_key in da.ENV_VAR_DICT
    assert "VIDEO_GEN_BACKEND" in da.ENV_VAR_DICT
    for opt_var in (
        "OMNIFLASH_VIDEO_MODEL",
        "VEO_VIDEO_MODEL",
        "VIDEO_TTS_MODEL",
        "VIDEO_MUSIC_MODEL",
    ):
        assert opt_var in da._OPTIONAL_FORWARDED_ENV_VARS


def test_storyboard_issues_clean_and_all_deterministic_violations():
    state = _base_campaign_state()
    clean = _valid_storyboard_dict()
    assert video_qa.storyboard_issues(clean, state) == []

    # 1. Hero 30s sum mismatch (28s) + non-sequential shot numbers
    bad_hero = _valid_storyboard_dict()
    bad_hero["shots"][4]["duration_seconds"] = 6  # 32s total
    bad_hero["shots"][2]["shot_number"] = 9
    hero_issues = video_qa.storyboard_issues(bad_hero, state)
    assert any("Hero 30s cut duration sum is 32s" in i for i in hero_issues)
    assert any("must be sequential" in i for i in hero_issues)

    # 2. Social 15s cut < 2 shots and duration outside 14..16s
    bad_social = _valid_storyboard_dict()
    bad_social["shots"][1]["include_in_15s"] = False  # leaves only 1 shot (6s)
    social_issues = video_qa.storyboard_issues(bad_social, state)
    assert any("Social 15s cut selects 1 shot" in i for i in social_issues)
    assert any("Social 15s cut duration sum is 6s" in i for i in social_issues)

    # 3. Bumper 6s cut 0 shots or wrong duration
    bad_bumper = _valid_storyboard_dict()
    bad_bumper["shots"][0]["include_in_6s"] = False
    bad_bumper["shots"][1]["include_in_6s"] = True  # 8s shot
    bumper_issues = video_qa.storyboard_issues(bad_bumper, state)
    assert any("Bumper 6s cut duration sum is 8s" in i for i in bumper_issues)

    # 4. Voiceover word rate > ceil(2.3 * duration_seconds)
    bad_vo = _valid_storyboard_dict()
    bad_vo["shots"][4]["voiceover_line"] = (
        "One two three four five six seven eight nine ten eleven twelve"  # 12 words in a 4s shot (max ceil(9.2) = 10 words)
    )
    vo_issues = video_qa.storyboard_issues(bad_vo, state)
    assert any("voiceover_line has 12 words (max 10 words" in i for i in vo_issues)

    # 5. Unknown anchor_concept_name
    bad_anchor = _valid_storyboard_dict()
    bad_anchor["shots"][0]["anchor_concept_name"] = "Invented Concept"
    anchor_issues = video_qa.storyboard_issues(bad_anchor, state)
    assert any(
        "does not match any finalized visual concept" in i for i in anchor_issues
    )

    # 6. Unapproved on_screen_super
    bad_super = _valid_storyboard_dict()
    bad_super["shots"][0]["on_screen_super"] = "Flash Sale 90% Off"
    super_issues = video_qa.storyboard_issues(bad_super, state)
    assert any("on_screen_super" in i and "does not match" in i for i in super_issues)

    # 7. Avoided terms from brief.avoid, brief.brand.do_not, and visual_avoid
    extracted_avoid = video_qa.extract_avoid_terms(state)
    assert "slapstick falls" in extracted_avoid
    assert "guaranteed miracles" in extracted_avoid
    assert "neon skulls" in extracted_avoid

    bad_avoid = _valid_storyboard_dict()
    bad_avoid["shots"][0]["visual_action_prompt"] = (
        "Runner glides past neon skulls in Rocket Skates."
    )
    bad_avoid["shots"][1]["voiceover_line"] = "Experience guaranteed miracles tonight."
    bad_avoid["shots"][2]["visual_action_prompt"] = (
        "Comedic slapstick falls on pavement."
    )
    avoid_issues = video_qa.storyboard_issues(bad_avoid, state)
    assert any("neon skulls" in i for i in avoid_issues)
    assert any("guaranteed miracles" in i for i in avoid_issues)
    assert any("slapstick falls" in i for i in avoid_issues)


def test_storyboard_gate_bounded_revision_and_failsoft_rollback():
    bad = _valid_storyboard_dict()
    bad["shots"][0]["anchor_concept_name"] = "Nonexistent Concept"
    state = _base_campaign_state(video_storyboard=bad)

    route, delta = ca.storyboard_gate_decision(state, max_rounds=1)
    assert route == "revise"
    assert delta["video_storyboard_revision_rounds_used"] == 1
    assert delta["video_storyboard__before_revision"] == bad
    assert "Nonexistent Concept" in delta["video_storyboard_issues"]

    # Budget spent -> exits "ok" and records residual issues in video_storyboard__issues
    spent_state = {**state, **delta}
    route2, delta2 = ca.storyboard_gate_decision(spent_state, max_rounds=1)
    assert route2 == "ok"
    assert delta2["video_storyboard_issues"] == ""
    assert delta2["video_storyboard__issues"]
    assert any("Nonexistent Concept" in i for i in delta2["video_storyboard__issues"])

    # Fail-soft rollback restores pre-revision storyboard and records residual issues
    corrupted_state = {
        **spent_state,
        "video_storyboard": {"shots": []},
    }
    err_delta = ca._video_storyboard_reviser_failed(
        corrupted_state, RuntimeError("boom")
    )
    assert err_delta["video_storyboard"] == bad
    assert err_delta["video_storyboard__before_revision"] is None
    assert any(
        "Nonexistent Concept" in i for i in err_delta["video_storyboard__issues"]
    )


def test_video_director_prompt_tokens_are_all_optional_and_brace_clean():
    instr = prompts.VIDEO_DIRECTOR_INSTR
    tokens = re.findall(r"\{([^{}]*)\}", instr)
    assert tokens
    for tok in tokens:
        assert re.fullmatch(r"[A-Za-z_]\w*\?", tok), (
            f"VIDEO_DIRECTOR_INSTR token {{{tok}}} must use optional '?'"
        )
    stripped = re.sub(r"\{[^{}]*\}", "", instr)
    assert "{" not in stripped and "}" not in stripped


def test_video_production_disabled_by_default_zero_cost_skip(monkeypatch):
    from creative_agent import video_tools

    # 1. _set_initial_states seeds video_production_enabled = False by default
    ctx = SimpleNamespace(state={})
    callbacks._set_initial_states({}, ctx.state)
    assert ctx.state["video_production_enabled"] is False

    # 2. video_gate routes to "skip" unless video_production_enabled is truthy
    assert ca.video_gate(SimpleNamespace(state={})).actions.route == "skip"
    assert (
        ca.video_gate(
            SimpleNamespace(state={"video_production_enabled": False})
        ).actions.route
        == "skip"
    )
    assert (
        ca.video_gate(
            SimpleNamespace(state={"video_production_enabled": "false"})
        ).actions.route
        == "skip"
    )
    assert (
        ca.video_gate(
            SimpleNamespace(state={"video_production_enabled": True})
        ).actions.route
        == "produce"
    )

    # 3. video_storyboard_enabled_gate skips LLM storyboard director when disabled
    assert (
        ca.video_storyboard_enabled_gate(SimpleNamespace(state={})).actions.route
        == "skip"
    )
    skip_msg = ca.video_storyboard_skipped(SimpleNamespace(state={}))
    assert "skipped" in skip_msg.lower()

    # 4. produce_video_reel performs zero client/FFmpeg work when disabled
    def _forbidden_client():
        raise AssertionError("GenAI client must not be constructed when disabled")

    monkeypatch.setattr(video_tools, "_get_video_genai_client", _forbidden_client)
    res = asyncio.run(
        video_tools.produce_video_reel(
            SimpleNamespace(state={"video_production_enabled": False})
        )
    )
    assert res["status"] == "skipped"


def test_checkpoint_4_review_tool_and_resume_edit_validation():
    assert isinstance(review_video_storyboard_tool, LongRunningFunctionTool)
    assert review_video_storyboard_tool.name == "review_video_storyboard"
    tool_ctx = SimpleNamespace(actions=SimpleNamespace(skip_summarization=False))
    pending = review_video_storyboard(tool_ctx)
    assert pending is None
    assert tool_ctx.actions.skip_summarization is True

    # Verify reset_video_state clears stale rendered videos and idempotency flag
    stale_ctx = SimpleNamespace(
        state={
            "video_storyboard": _valid_storyboard_dict(),
            "_videos_generated": True,
            "generated_videos": {"hero_30s_16x9": {"gcs_uri": "gs://b/old.mp4"}},
            "generated_videos__issues": ["old conform issue"],
            "video_qa__issues": ["old qa issue"],
        }
    )
    callbacks.reset_video_state(stale_ctx)
    assert stale_ctx.state["video_storyboard"] is None
    assert stale_ctx.state["_videos_generated"] is False
    assert stale_ctx.state["generated_videos"] is None
    assert stale_ctx.state["generated_videos__issues"] is None
    assert stale_ctx.state["video_qa__issues"] is None

    # Test pure merge_storyboard_edit + HTTP /resume 400 vs 200
    svc = InMemorySessionService()
    resumed_calls: list[str] = []

    class _FakeRunner:
        async def run_async(self, *, user_id, session_id, new_message):
            resumed_calls.append(session_id)
            if False:
                yield None

    async_runs.configure(
        session_service=svc,
        runner_factory=lambda _app: _FakeRunner(),
        authz_mode=AuthzMode.TRUST_CLIENT,
    )
    app = FastAPI()
    app.include_router(async_runs.router)

    async def _exercise():
        initial_sb = _valid_storyboard_dict()
        sess = await svc.create_session(
            app_name="interactive_creative",
            user_id="u1",
            session_id="s-video-cp4",
            state=_base_campaign_state(
                video_production_enabled=True,
                video_storyboard=initial_sb,
                video_storyboard__issues=["stale issue"],
                _videos_generated=True,
            ),
        )

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            # Invalid storyboard edit (bad duration sum 26s) -> HTTP 400 before claim
            bad_sb = _valid_storyboard_dict()
            bad_sb["shots"][1]["duration_seconds"] = 4  # 26s hero total
            bad_resp = await client.post(
                f"/runs/interactive_creative/u1/{sess.id}/resume",
                json={
                    "functionCallId": "fc-cp4",
                    "functionName": "review_video_storyboard",
                    "response": {"approved": True},
                    "edits": [{"field": "video_storyboard", "value": bad_sb}],
                },
            )
            assert bad_resp.status_code == 400
            detail = bad_resp.json()["detail"]
            assert detail["reason"] == "invalid_storyboard"
            assert any(
                "Hero 30s cut duration sum" in e["msg"] for e in detail["errors"]
            )
            assert resumed_calls == []

            # Invalid anchor concept against session.state -> HTTP 400 before claim
            bad_anchor_sb = _valid_storyboard_dict()
            bad_anchor_sb["shots"][0]["anchor_concept_name"] = "Unknown Visual"
            bad_anchor_resp = await client.post(
                f"/runs/interactive_creative/u1/{sess.id}/resume",
                json={
                    "functionCallId": "fc-cp4",
                    "functionName": "review_video_storyboard",
                    "response": {"approved": True},
                    "edits": [{"field": "video_storyboard", "value": bad_anchor_sb}],
                },
            )
            assert bad_anchor_resp.status_code == 400
            assert bad_anchor_resp.json()["detail"]["reason"] == "invalid_storyboard"
            assert resumed_calls == []

            # Valid edited storyboard -> HTTP 200, merged into state, stale issues cleared
            edited_sb = _valid_storyboard_dict(title="Director Cut Midnight Commute")
            ok_resp = await client.post(
                f"/runs/interactive_creative/u1/{sess.id}/resume",
                json={
                    "functionCallId": "fc-cp4",
                    "functionName": "review_video_storyboard",
                    "response": {"approved": True},
                    "edits": [{"field": "video_storyboard", "value": edited_sb}],
                },
            )
            assert ok_resp.status_code == 200
            await asyncio.sleep(0.05)

            updated = await svc.get_session(
                app_name="interactive_creative",
                user_id="u1",
                session_id=sess.id,
            )
            assert updated is not None
            assert (
                updated.state["video_storyboard"]["title"]
                == "Director Cut Midnight Commute"
            )
            assert updated.state["video_storyboard_edited"] is True
            assert not updated.state.get("video_storyboard__issues")
            assert updated.state["_videos_generated"] is False

    asyncio.run(_exercise())


def test_observability_gallery_finalize_and_changelog_video_surfaces():
    # 1. Degradation warning labels
    warnings = collect_degradation_warnings(
        {
            "video_storyboard__issues": ["Shot 1 VO trimmed"],
            "video_qa__issues": ["Shot 2 minor motion blur"],
            "generated_videos__issues": ["Bumper music fallback used"],
        }
    )
    assert any("Video storyboard" in w for w in warnings)
    assert any("Video check" in w for w in warnings)
    assert any("Video reel" in w for w in warnings)

    # 2. Optional HTML gallery Video Reel section
    assert _build_video_reel_html(None) == ""
    assert _build_video_reel_html({}) == ""
    videos = {
        "hero_30s_16x9": {
            "cut": "hero_30s_16x9",
            "label": "30s Hero (16:9)",
            "aspect_ratio": "16:9",
            "duration_seconds": 30,
            "backend": "omniflash",
            "gcs_uri": "gs://bucket/folder/run/video_hero_30s_16x9.mp4",
            "voiceover_transcript": "Step into <Rocket Skates> today.",
            "shot_numbers": [1, 2, 3, 4, 5],
        }
    }
    html_block = _build_video_reel_html(videos)
    assert "<h2>Video Reel</h2>" in html_block
    assert "30s Hero (16:9)" in html_block
    assert "5 shot(s)" in html_block
    assert "&lt;Rocket Skates&gt;" in html_block
    assert (
        "https://storage.mtls.cloud.google.com/bucket/folder/run/video_hero_30s_16x9.mp4"
        in html_block
    )

    # 3. Finalize summary mentions rendered video cuts only when present
    no_vid_summary = finalize_summary({})
    assert "Video reel:" not in no_vid_summary
    with_vid_summary = finalize_summary({"generated_videos": videos})
    assert "Video reel: 1 cut(s) rendered (hero_30s_16x9)." in with_vid_summary

    # 4. Changelog includes storyboard gate, Checkpoint 4 edit, and video reel milestones
    log_payload = changelog.build_changelog(
        "interactive_creative",
        "sess-1",
        {
            "brand": "Acme",
            "video_storyboard": _valid_storyboard_dict(),
            "video_storyboard_revision_rounds_used": 1,
            "video_storyboard_edited": True,
            "generated_videos": videos,
        },
    )
    assert set(log_payload["gate_rounds"]) == {"brief", "ad_copy", "visual_concept"}
    labels = [e["label"] for e in log_payload["entries"]]
    assert "Edited video storyboard at review checkpoint" in labels
    assert "Finalized & gated video storyboard" in labels
    assert "Rendered multi-cut video reel" in labels
