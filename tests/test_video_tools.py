"""Tests for Phase 3 video rendering, Veo fallback, shot QA retakes, TTS/music, and FFmpeg conform."""

from __future__ import annotations

import asyncio
import base64
import io
import subprocess
import wave
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from google.adk.artifacts import InMemoryArtifactService
from google.adk.sessions import InMemorySessionService
from google.genai import errors as genai_errors
from google.genai import types

from creative_agent import VideoStoryboard, references, video_qa, video_tools
from creative_agent.video_tools import ReferenceImage
from runserver import delivery_kit
from runserver.authz import AuthzMode

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def _make_synthetic_mp4(
    path: Path, *, duration: float = 1.0, color: str = "navy"
) -> bytes:
    """Create a real tiny H.264 MP4 clip via `imageio-ffmpeg` for conform integration tests."""
    ffmpeg_bin = video_tools.resolve_ffmpeg_binary()
    proc = subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x240:r=24:d={duration}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert proc.returncode == 0
    data = path.read_bytes()
    assert b"ftyp" in data[:32]
    return data


def _probe_streams(mp4_bytes: bytes, tmp_path: Path, name: str) -> str:
    """Run `ffmpeg -i` on an MP4 buffer and return stderr stream metadata."""
    target = tmp_path / name
    target.write_bytes(mp4_bytes)
    proc = subprocess.run(
        [video_tools.resolve_ffmpeg_binary(), "-hide_banner", "-i", str(target)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return proc.stderr


def test_role_gated_video_references_and_omniflash_bindings():
    refs = [
        ReferenceImage(role="product", data=_TINY_PNG, mime_type="image/png"),
        ReferenceImage(role="cast", data=_TINY_PNG, mime_type="image/png"),
        ReferenceImage(role="logo", data=_TINY_PNG, mime_type="image/png"),
        ReferenceImage(role="style", data=_TINY_PNG, mime_type="image/png"),
    ]

    # product_hero and graphic_lockup MUST omit "cast" (and non-video logo/style roles)
    for non_human_mode in ("product_hero", "graphic_lockup"):
        selected = video_tools.select_video_references_for_concept(refs, non_human_mode)
        assert [r.role for r in selected] == ["product"]

    # cast_with_product and product_in_scene keep both "product" and "cast"
    for human_mode in ("cast_with_product", "product_in_scene"):
        selected = video_tools.select_video_references_for_concept(refs, human_mode)
        assert [r.role for r in selected] == ["product", "cast"]

    items, prompt_text = video_tools.build_omniflash_shot_inputs(
        {
            "shot_number": 1,
            "duration_seconds": 6,
            "camera_motion": "Dolly in",
            "visual_action_prompt": "Talent laces Rocket Skates.",
            "on_screen_super": "Own Midnight",
            "audio_cue": "City ambience",
        },
        concept={
            "concept_name": "Neon Sprint",
            "visual_style": "anodized neon cinema",
            "composition_mode": "cast_with_product",
        },
        keyframe_bytes=_TINY_PNG,
        keyframe_mime="image/png",
        references=video_tools.select_video_references_for_concept(
            refs, "cast_with_product"
        ),
        brand="Acme",
        target_product="Rocket Skates",
        reference_description="Matte carbon skate frame with cyan LED wheels.",
    )
    text_tags = [it["text"] for it in items if it["type"] == "text"]
    assert "<FIRST_FRAME>" in text_tags
    assert any("<IMAGE_REF_0> (product reference)" in t for t in text_tags)
    assert any("<IMAGE_REF_1> (cast reference)" in t for t in text_tags)
    assert '"Own Midnight"' in prompt_text
    assert "<FIRST_FRAME>" in prompt_text


@pytest.mark.parametrize("status_code", [403, 404, 503])
def test_omniflash_primary_and_auto_veo_fallback_on_403_404_5xx(
    monkeypatch, status_code
):
    async def _fast_sleep(_secs: float) -> None:
        return None

    monkeypatch.setattr(video_tools, "_async_sleep", _fast_sleep)
    monkeypatch.setattr(
        video_qa,
        "inspect_video_shot",
        lambda *_a, **_kw: video_qa.VideoQAVerdict(
            product_consistent=True,
            text_legible=True,
            motion_coherent=True,
            avoid_respected=True,
        ),
    )

    fake_mp4 = b"\x00\x00\x00\x18ftypmp42synthetic-veo-bytes"
    veo_calls: list[dict[str, Any]] = []

    class _FakeInteractionsFailing:
        def create(self, **_kwargs):
            if status_code >= 500:
                raise genai_errors.ServerError(
                    status_code, {"error": {"message": "unavailable"}}
                )
            raise genai_errors.ClientError(
                status_code, {"error": {"message": "not allowlisted"}}
            )

    class _FakeModels:
        def generate_videos(self, **kwargs):
            veo_calls.append(kwargs)
            return SimpleNamespace(
                name="op-veo-1", done=False, response=None, error=None
            )

    class _FakeOperations:
        def get(self, _op):
            return SimpleNamespace(
                done=True,
                error=None,
                response=SimpleNamespace(
                    generated_videos=[
                        SimpleNamespace(
                            video=SimpleNamespace(video_bytes=fake_mp4, uri=None)
                        )
                    ]
                ),
            )

    fake_client = SimpleNamespace(
        interactions=_FakeInteractionsFailing(),
        models=_FakeModels(),
        operations=_FakeOperations(),
    )

    shot_spec = {
        "shot_number": 1,
        "duration_seconds": 6,
        "camera_motion": "Push in",
        "visual_action_prompt": "Rocket Skates hero shot.",
        "voiceover_line": "Step into speed.",
        "on_screen_super": "",
        "include_in_15s": True,
        "include_in_6s": True,
    }

    # 1. VIDEO_GEN_BACKEND="auto" transparently falls back to Veo 3.1
    rendered, next_backend = asyncio.run(
        video_tools.render_and_qa_shot(
            fake_client,
            shot=shot_spec,
            concept={"concept_name": "Hero", "composition_mode": "product_hero"},
            keyframe_bytes=_TINY_PNG,
            keyframe_mime="image/png",
            references=[],
            requested_backend="auto",
            active_backend="omniflash",
            brand="Acme",
            target_product="Rocket Skates",
            avoid_terms=[],
            reference_description="",
            max_retakes=1,
        )
    )
    assert rendered.backend == "veo"
    assert next_backend == "veo"
    assert rendered.mp4_bytes == fake_mp4
    assert len(veo_calls) == 1

    # 2. Explicit VIDEO_GEN_BACKEND="omniflash" does NOT silently fall back
    with pytest.raises((genai_errors.ClientError, genai_errors.ServerError)):
        asyncio.run(
            video_tools.render_and_qa_shot(
                fake_client,
                shot=shot_spec,
                concept={"concept_name": "Hero", "composition_mode": "product_hero"},
                keyframe_bytes=_TINY_PNG,
                keyframe_mime="image/png",
                references=[],
                requested_backend="omniflash",
                active_backend="omniflash",
                brand="Acme",
                target_product="Rocket Skates",
                avoid_terms=[],
                reference_description="",
                max_retakes=1,
            )
        )

    # 3. Explicit VIDEO_GEN_BACKEND="veo" calls generate_videos directly without interactions.create
    veo_only_client = SimpleNamespace(
        interactions=SimpleNamespace(
            create=lambda **_kw: (_ for _ in ()).throw(
                AssertionError("interactions.create must not run for veo backend")
            )
        ),
        models=_FakeModels(),
        operations=_FakeOperations(),
    )
    rendered_veo, next_veo = asyncio.run(
        video_tools.render_and_qa_shot(
            veo_only_client,
            shot=shot_spec,
            concept={"concept_name": "Hero", "composition_mode": "product_hero"},
            keyframe_bytes=_TINY_PNG,
            keyframe_mime="image/png",
            references=[],
            requested_backend="veo",
            active_backend="veo",
            brand="Acme",
            target_product="Rocket Skates",
            avoid_terms=[],
            reference_description="",
            max_retakes=1,
        )
    )
    assert rendered_veo.backend == "veo"
    assert next_veo == "veo"
    assert rendered_veo.mp4_bytes == fake_mp4


def test_omniflash_polling_and_stateful_conversational_diff_retake_and_regression_keep(
    monkeypatch,
):
    async def _fast_sleep(_secs: float) -> None:
        return None

    monkeypatch.setattr(video_tools, "_async_sleep", _fast_sleep)

    clip_v1 = b"\x00\x00\x00\x18ftypmp42clip-attempt-1"
    clip_v2 = b"\x00\x00\x00\x18ftypmp42clip-attempt-2"
    create_calls: list[dict[str, Any]] = []

    class _FakeInteractions:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            idx = len(create_calls)
            if idx == 1:
                return SimpleNamespace(
                    id="ix-shot1-v1",
                    status="in_progress",
                    output_video=None,
                )
            return SimpleNamespace(
                id=f"ix-shot1-v{idx}",
                status="completed",
                output_video=SimpleNamespace(
                    data=base64.b64encode(clip_v2).decode("ascii"),
                    uri=None,
                ),
            )

        def get(self, interaction_id: str):
            assert interaction_id == "ix-shot1-v1"
            return SimpleNamespace(
                id="ix-shot1-v1",
                status="completed",
                output_video=SimpleNamespace(
                    data=base64.b64encode(clip_v1).decode("ascii"),
                    uri=None,
                ),
            )

    fake_client = SimpleNamespace(interactions=_FakeInteractions())
    shot_spec = {
        "shot_number": 1,
        "duration_seconds": 6,
        "camera_motion": "Dolly push",
        "visual_action_prompt": "Rocket Skates glide under streetlight.",
        "voiceover_line": "Own the night.",
        "on_screen_super": "Own Midnight",
        "include_in_15s": True,
        "include_in_6s": True,
    }

    # Case A: Retake improves QA score (3/4 -> 4/4) -> retake clip is kept
    verdicts_improve = iter(
        [
            video_qa.VideoQAVerdict(
                product_consistent=True,
                text_legible=False,
                motion_coherent=True,
                avoid_respected=True,
                defect_instruction="Crisp white typography reading 'Own Midnight'.",
            ),
            video_qa.VideoQAVerdict(
                product_consistent=True,
                text_legible=True,
                motion_coherent=True,
                avoid_respected=True,
            ),
        ]
    )
    monkeypatch.setattr(
        video_qa, "inspect_video_shot", lambda *_a, **_kw: next(verdicts_improve)
    )

    rendered_ok, _ = asyncio.run(
        video_tools.render_and_qa_shot(
            fake_client,
            shot=shot_spec,
            concept={
                "concept_name": "Neon Sprint",
                "composition_mode": "product_in_scene",
            },
            keyframe_bytes=_TINY_PNG,
            keyframe_mime="image/png",
            references=[],
            requested_backend="omniflash",
            active_backend="omniflash",
            brand="Acme",
            target_product="Rocket Skates",
            avoid_terms=[],
            reference_description="",
            max_retakes=1,
        )
    )
    assert len(create_calls) == 2
    assert create_calls[0]["store"] is True
    assert create_calls[1]["store"] is True
    assert create_calls[1]["previous_interaction_id"] == "ix-shot1-v1"
    retake_text = create_calls[1]["input"][0]["text"]
    assert "[PRESERVATION LOCK]" in retake_text
    assert "[ISOLATED DIFF]" in retake_text
    assert rendered_ok.mp4_bytes == clip_v2
    assert rendered_ok.retakes_used == 1
    assert video_qa.qa_passed(rendered_ok.qa_verdict)

    # Case B: Retake regresses QA score (3/4 -> 1/4) -> baseline clip_v1 is kept!
    create_calls.clear()
    verdicts_regress = iter(
        [
            video_qa.VideoQAVerdict(
                product_consistent=True,
                text_legible=False,
                motion_coherent=True,
                avoid_respected=True,
                defect_instruction="Fix super kerning.",
            ),
            video_qa.VideoQAVerdict(
                product_consistent=False,
                text_legible=False,
                motion_coherent=False,
                avoid_respected=True,
                defect_instruction="Severe blur.",
            ),
        ]
    )
    monkeypatch.setattr(
        video_qa, "inspect_video_shot", lambda *_a, **_kw: next(verdicts_regress)
    )
    rendered_keep_base, _ = asyncio.run(
        video_tools.render_and_qa_shot(
            fake_client,
            shot=shot_spec,
            concept={
                "concept_name": "Neon Sprint",
                "composition_mode": "product_in_scene",
            },
            keyframe_bytes=_TINY_PNG,
            keyframe_mime="image/png",
            references=[],
            requested_backend="omniflash",
            active_backend="omniflash",
            brand="Acme",
            target_product="Rocket Skates",
            avoid_terms=[],
            reference_description="",
            max_retakes=1,
        )
    )
    assert rendered_keep_base.mp4_bytes == clip_v1
    assert rendered_keep_base.retakes_used == 1
    assert not video_qa.qa_passed(rendered_keep_base.qa_verdict)


def test_tts_pcm_wrapping_and_music_failsoft_never_touches_live_music():
    # 1. Raw PCM bytes wrapped into valid 24 kHz mono 16-bit RIFF WAV
    raw_pcm = b"\x00\x01" * 2400  # 0.1s @ 24kHz 16-bit mono
    wav_bytes = video_tools.wrap_pcm_as_wav(raw_pcm, sample_rate=24000)
    assert wav_bytes[:4] == b"RIFF"
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 24000
        assert wf.getnframes() == 2400

    # 2. TTS model returns raw PCM inline_data -> synthesize_voiceover_clip_sync wraps to WAV
    class _FakeTTSModels:
        def generate_content(self, **_kwargs):
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        content=SimpleNamespace(
                            parts=[
                                SimpleNamespace(
                                    inline_data=SimpleNamespace(
                                        data=raw_pcm,
                                        mime_type="audio/L16;rate=24000",
                                    )
                                )
                            ]
                        )
                    )
                ]
            )

    class _TrapAio:
        @property
        def live(self):
            raise AssertionError("client.aio.live.music must NEVER be accessed")

    tts_client = SimpleNamespace(models=_FakeTTSModels(), aio=_TrapAio())
    vo_pcm = video_tools.synthesize_voiceover_clip_sync(
        "Step into Rocket Skates.",
        duration_seconds=0.5,
        voiceover_style="Crisp commercial narrator",
        voice_name="Kore",
        client=tts_client,
    )
    assert vo_pcm is not None and len(vo_pcm) == int(0.5 * 24000) * 2
    vo_wav = video_tools.wrap_pcm_as_wav(vo_pcm)
    assert vo_wav[:4] == b"RIFF"

    # 3. Fail-soft TTS + fail-soft music bed fallback (without touching client.aio.live)
    class _FailingAudioModels:
        def generate_content(self, **_kwargs):
            raise RuntimeError("audio endpoint unavailable")

    failing_client = SimpleNamespace(
        models=_FailingAudioModels(),
        interactions=SimpleNamespace(
            create=lambda **_kw: (_ for _ in ()).throw(
                RuntimeError("lyria unavailable")
            )
        ),
        aio=_TrapAio(),
    )
    assert (
        video_tools.synthesize_voiceover_clip_sync(
            "Test line",
            duration_seconds=0.5,
            voiceover_style="",
            voice_name="Kore",
            client=failing_client,
        )
        is None
    )
    fallback_music, used_model = asyncio.run(
        video_tools.synthesize_music_wav(
            "Warm minimal synth pulse",
            duration_seconds=2.0,
            client=failing_client,
        )
    )
    assert used_model is False
    assert fallback_music[:4] == b"RIFF"
    with wave.open(io.BytesIO(fallback_music), "rb") as mwf:
        assert mwf.getframerate() == 24000
        assert mwf.getnframes() == 48000


def test_end_to_end_ffmpeg_multicut_conform_and_delivery_kit_zip(tmp_path, monkeypatch):
    """Integration test using real `imageio-ffmpeg` static binary to conform all 3 cuts
    (16:9 hero, 9:16 social, 9:16 bumper) with sidechain ducking + EBU R128 loudnorm
    and export them inside the Delivery Kit `.zip`."""
    clip_a = _make_synthetic_mp4(tmp_path / "shot_a.mp4", duration=0.6, color="navy")
    clip_b = _make_synthetic_mp4(tmp_path / "shot_b.mp4", duration=0.6, color="teal")
    clip_c = _make_synthetic_mp4(
        tmp_path / "shot_c.mp4", duration=0.6, color="darkslateblue"
    )
    clip_d = _make_synthetic_mp4(
        tmp_path / "shot_d.mp4", duration=0.6, color="midnightblue"
    )
    clips_by_shot = {1: clip_a, 2: clip_b, 3: clip_c, 4: clip_d}

    raw_pcm = b"\x10\x02" * 6000  # 0.25s spoken pulse @ 24kHz

    async def _fake_render_and_qa_shot(_client, *, shot, **_kwargs):
        s_num = int(shot["shot_number"])
        verdict = video_qa.VideoQAVerdict(
            product_consistent=True,
            text_legible=True,
            motion_coherent=(s_num != 3),  # Shot 3 leaves an advisory residual issue
            avoid_respected=True,
            defect_instruction="Slight wheel motion blur." if s_num == 3 else "",
        )
        return (
            video_tools.RenderedShot(
                shot_number=s_num,
                duration_seconds=0.6,
                mp4_bytes=clips_by_shot[s_num],
                backend="omniflash",
                interaction_id=f"ix-{s_num}",
                qa_verdict=verdict,
                retakes_used=1 if s_num == 3 else 0,
                voiceover_line=str(shot.get("voiceover_line") or ""),
                include_in_15s=bool(shot.get("include_in_15s")),
                include_in_6s=bool(shot.get("include_in_6s")),
            ),
            "omniflash",
        )

    monkeypatch.setattr(video_tools, "render_and_qa_shot", _fake_render_and_qa_shot)
    monkeypatch.setattr(
        video_tools,
        "_get_video_genai_client",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        video_tools,
        "synthesize_voiceover_clip_sync",
        lambda *_a, **_kw: raw_pcm,
    )

    async def _fake_music(*_a, **_kw):
        return video_tools.generate_fallback_ambient_wav(duration_seconds=3.0), False

    monkeypatch.setattr(video_tools, "synthesize_music_wav", _fake_music)
    monkeypatch.setattr(
        video_tools, "_upload_video_bytes_to_gcs", lambda *_a, **_kw: ""
    )

    artifact_svc = InMemoryArtifactService()
    session_svc = InMemorySessionService()

    storyboard = {
        "title": "Compact Integration Reel",
        "creative_direction": "Hook, proof, urban glide, resolve.",
        "music_direction": "Warm ambient pulse, strictly instrumental.",
        "voiceover_style": "Crisp narrator",
        "shots": [
            {
                "shot_number": 1,
                "duration_seconds": 6,
                "anchor_concept_name": "Neon Sprint",
                "camera_motion": "Dolly push",
                "visual_action_prompt": "Opening glide.",
                "voiceover_line": "Own the midnight commute.",
                "on_screen_super": "Own Midnight",
                "audio_cue": "Whoosh",
                "include_in_15s": True,
                "include_in_6s": True,
            },
            {
                "shot_number": 2,
                "duration_seconds": 8,
                "anchor_concept_name": "Carbon Close-Up",
                "camera_motion": "Macro pan",
                "visual_action_prompt": "Carbon chassis lock.",
                "voiceover_line": "Precision carbon frame.",
                "on_screen_super": "",
                "audio_cue": "Latch click",
                "include_in_15s": True,
                "include_in_6s": False,
            },
            {
                "shot_number": 3,
                "duration_seconds": 8,
                "anchor_concept_name": "Neon Sprint",
                "camera_motion": "Tracking lateral follow",
                "visual_action_prompt": "Runner accelerates across plaza.",
                "voiceover_line": "Turn blocks into seconds.",
                "on_screen_super": "",
                "audio_cue": "Synth swell",
                "include_in_15s": False,
                "include_in_6s": False,
            },
            {
                "shot_number": 4,
                "duration_seconds": 8,
                "anchor_concept_name": "Hero Lockup",
                "camera_motion": "Static pedestal",
                "visual_action_prompt": "Rocket Skates hero lockup.",
                "voiceover_line": "Ride Rocket Skates now.",
                "on_screen_super": "Ride Now",
                "audio_cue": "Chime",
                "include_in_15s": False,
                "include_in_6s": False,
            },
        ],
    }
    VideoStoryboard.model_validate(storyboard)

    async def _run_pipeline_and_kit():
        state: dict[str, Any] = {
            "video_production_enabled": True,
            "brand": "Acme",
            "target_product": "Rocket Skates",
            "gcs_folder": "test_folder",
            "agent_output_dir": "run_001",
            "video_storyboard": storyboard,
            "final_visual_concepts": {
                "visual_concepts": [
                    {
                        "concept_name": "Neon Sprint",
                        "composition_mode": "cast_with_product",
                    },
                    {
                        "concept_name": "Carbon Close-Up",
                        "composition_mode": "product_hero",
                    },
                    {
                        "concept_name": "Hero Lockup",
                        "composition_mode": "graphic_lockup",
                    },
                ]
            },
            "generated_images": {},
        }

        async def _save_artifact(filename: str, artifact: types.Part) -> int:
            return await artifact_svc.save_artifact(
                app_name="creative_agent",
                user_id="u1",
                session_id="s-reel",
                filename=filename,
                artifact=artifact,
            )

        async def _load_artifact(filename: str) -> types.Part | None:
            return await artifact_svc.load_artifact(
                app_name="creative_agent",
                user_id="u1",
                session_id="s-reel",
                filename=filename,
            )

        ctx = SimpleNamespace(
            state=state,
            save_artifact=_save_artifact,
            load_artifact=_load_artifact,
        )
        result = await video_tools.produce_video_reel(ctx)
        assert result["status"] == "success"
        assert set(result["cuts"]) == {
            "hero_30s_16x9",
            "social_15s_9x16",
            "bumper_6s_9x16",
        }
        assert state["_videos_generated"] is True
        # Shot 3's residual QA failure was recorded on video_qa__issues
        assert any("Shot 3" in issue for issue in state["video_qa__issues"])

        # Inspect conformed MP4 artifacts via `ffmpeg -i` for exact stream geometry + AAC audio
        expected_dims = {
            "hero_30s_16x9": "1280x720",
            "social_15s_9x16": "720x1280",
            "bumper_6s_9x16": "720x1280",
        }
        for cut_key, dims in expected_dims.items():
            akey = state["generated_videos"][cut_key]["artifact_key"]
            part = await _load_artifact(akey)
            assert part is not None and part.inline_data is not None
            mp4_data = bytes(part.inline_data.data)
            assert b"ftyp" in mp4_data[:32]
            probe_info = _probe_streams(mp4_data, tmp_path, f"probe_{cut_key}.mp4")
            assert dims in probe_info, (cut_key, probe_info)
            assert "Video: h264" in probe_info
            assert "Audio: aac" in probe_info

        # Verify Delivery Kit .zip exports all 3 conformed MP4s under videos/*.mp4
        await session_svc.create_session(
            app_name="creative_agent",
            user_id="u1",
            session_id="s-reel",
            state=state,
        )
        delivery_kit.configure(
            session_service=session_svc,
            artifact_service=artifact_svc,
            authz_mode=AuthzMode.TRUST_CLIENT,
            gcs_bucket="test-bucket",
        )
        app = FastAPI()
        app.include_router(delivery_kit.router)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            resp = await client.get("/runs/creative_agent/u1/s-reel/delivery-kit")
            assert resp.status_code == 200
            with zipfile.ZipFile(io.BytesIO(resp.content), "r") as zf:
                names = set(zf.namelist())
                assert "videos/hero-30s-16x9.mp4" in names
                assert "videos/social-15s-9x16.mp4" in names
                assert "videos/bumper-6s-9x16.mp4" in names
                assert b"ftyp" in zf.read("videos/hero-30s-16x9.mp4")[:32]

    asyncio.run(_run_pipeline_and_kit())


def test_conform_cut_with_ffmpeg_kills_and_reaps_subprocess_on_cancel(monkeypatch):
    events: list[str] = []
    started = asyncio.Event()

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Future()
            return b"", b""

        def kill(self) -> None:
            events.append("kill")

        async def wait(self) -> int:
            events.append("wait")
            self.returncode = -9
            return -9

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    async def _exercise_cancel():
        started.clear()
        events.clear()
        task = asyncio.create_task(
            video_tools.conform_cut_with_ffmpeg(
                [b"synthetic-shot"],
                width=1280,
                height=720,
                max_duration_seconds=6.0,
                ffmpeg_bin="/usr/bin/true",
                timeout_seconds=30.0,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events == ["kill", "wait"]

    async def _exercise_timeout():
        started.clear()
        events.clear()
        with pytest.raises(TimeoutError, match="FFmpeg conform timed out"):
            await video_tools.conform_cut_with_ffmpeg(
                [b"synthetic-shot"],
                width=1280,
                height=720,
                max_duration_seconds=6.0,
                ffmpeg_bin="/usr/bin/true",
                timeout_seconds=0.02,
            )
        assert events == ["kill", "wait"]

    asyncio.run(_exercise_cancel())
    asyncio.run(_exercise_timeout())


def test_produce_video_reel_per_cut_failsoft_ships_surviving_cuts(monkeypatch):
    fake_mp4 = b"\x00\x00\x00\x18ftypmp42synthetic-cut"

    async def _fake_render_and_qa_shot(_client, *, shot, **_kwargs):
        s_num = int(shot["shot_number"])
        return (
            video_tools.RenderedShot(
                shot_number=s_num,
                duration_seconds=float(shot["duration_seconds"]),
                mp4_bytes=fake_mp4,
                backend="omniflash",
                interaction_id=f"ix-{s_num}",
                qa_verdict=video_qa.VideoQAVerdict(
                    product_consistent=True,
                    text_legible=True,
                    motion_coherent=True,
                    avoid_respected=True,
                ),
                voiceover_line="",
                include_in_15s=bool(shot.get("include_in_15s")),
                include_in_6s=bool(shot.get("include_in_6s")),
            ),
            "omniflash",
        )

    async def _fake_music(*_a, **_kw):
        return None, False

    monkeypatch.setattr(video_tools, "render_and_qa_shot", _fake_render_and_qa_shot)
    monkeypatch.setattr(
        video_tools, "_get_video_genai_client", lambda: SimpleNamespace()
    )
    monkeypatch.setattr(video_tools, "synthesize_music_wav", _fake_music)
    monkeypatch.setattr(
        video_tools, "_upload_video_bytes_to_gcs", lambda *_a, **_kw: "gs://b/cut.mp4"
    )

    storyboard = {
        "title": "Fail-Soft Conform Reel",
        "creative_direction": "Hook, proof, resolve.",
        "music_direction": "Instrumental synth",
        "voiceover_style": "Warm narrator",
        "shots": [
            {
                "shot_number": 1,
                "duration_seconds": 6,
                "anchor_concept_name": "Hero",
                "camera_motion": "Dolly in",
                "visual_action_prompt": "Opening hero frame.",
                "include_in_15s": True,
                "include_in_6s": True,
            },
            {
                "shot_number": 2,
                "duration_seconds": 8,
                "anchor_concept_name": "Hero",
                "camera_motion": "Pan",
                "visual_action_prompt": "Mid beat.",
                "include_in_15s": True,
                "include_in_6s": False,
            },
            {
                "shot_number": 3,
                "duration_seconds": 8,
                "anchor_concept_name": "Hero",
                "camera_motion": "Orbit",
                "visual_action_prompt": "Proof beat.",
                "include_in_15s": False,
                "include_in_6s": False,
            },
            {
                "shot_number": 4,
                "duration_seconds": 8,
                "anchor_concept_name": "Hero",
                "camera_motion": "Hold",
                "visual_action_prompt": "Closing packshot.",
                "include_in_15s": False,
                "include_in_6s": False,
            },
        ],
    }

    # Case 1: Bumper (6s) FFmpeg conform fails -> Hero (30s) and Social (15s) still ship!
    async def _partial_fail_ffmpeg(*_a, max_duration_seconds: float, **_kw):
        if max_duration_seconds <= 6.5:
            raise RuntimeError("bumper encoder glitch")
        return fake_mp4

    monkeypatch.setattr(video_tools, "conform_cut_with_ffmpeg", _partial_fail_ffmpeg)
    partial_state: dict[str, Any] = {
        "video_production_enabled": True,
        "brand": "Acme",
        "target_product": "Rocket Skates",
        "video_storyboard": storyboard,
        "final_visual_concepts": {
            "visual_concepts": [
                {"concept_name": "Hero", "composition_mode": "product_hero"}
            ]
        },
    }
    partial_res = asyncio.run(
        video_tools.produce_video_reel(SimpleNamespace(state=partial_state))
    )
    assert partial_res["status"] == "success"
    assert partial_res["cuts"] == ["hero_30s_16x9", "social_15s_9x16"]
    assert partial_state["_videos_generated"] is True
    assert set(partial_state["generated_videos"].keys()) == {
        "hero_30s_16x9",
        "social_15s_9x16",
    }
    assert any(
        "bumper_6s_9x16 FFmpeg conform failed" in issue
        for issue in partial_state["generated_videos__issues"]
    )

    # Case 2: All 3 cuts fail FFmpeg conform -> status=="error", _videos_generated is False
    async def _all_fail_ffmpeg(*_a, **_kw):
        raise RuntimeError("ffmpeg binary crashed")

    monkeypatch.setattr(video_tools, "conform_cut_with_ffmpeg", _all_fail_ffmpeg)
    failed_state: dict[str, Any] = {
        "video_production_enabled": True,
        "brand": "Acme",
        "target_product": "Rocket Skates",
        "video_storyboard": storyboard,
        "final_visual_concepts": {
            "visual_concepts": [
                {"concept_name": "Hero", "composition_mode": "product_hero"}
            ]
        },
        "_videos_generated": False,
    }
    failed_res = asyncio.run(
        video_tools.produce_video_reel(SimpleNamespace(state=failed_state))
    )
    assert failed_res["status"] == "error"
    assert failed_state["_videos_generated"] is False
    assert failed_state["generated_videos"] is None
    assert len(failed_state["generated_videos__issues"]) == 3


def test_load_keyframe_bytes_enforces_bucket_confinement_traversal_and_max_bytes(
    monkeypatch,
):
    download_calls: list[tuple[str, str]] = []
    blobs_by_key: dict[tuple[str, str], Any] = {}

    class _FakeBlob:
        def __init__(
            self,
            bucket_name: str,
            blob_name: str,
            *,
            payload: bytes = b"ok-bytes",
            meta_size: int | None = None,
            reload_size: int | None = None,
        ) -> None:
            self._bucket_name = bucket_name
            self._blob_name = blob_name
            self._payload = payload
            self.size = meta_size
            self._reload_size = reload_size

        def reload(self) -> None:
            if self._reload_size is not None:
                self.size = self._reload_size

        def download_as_bytes(self) -> bytes:
            download_calls.append((self._bucket_name, self._blob_name))
            return self._payload

    class _FakeBucket:
        def __init__(self, name: str) -> None:
            self._name = name

        def blob(self, blob_name: str) -> _FakeBlob:
            return blobs_by_key.get(
                (self._name, blob_name),
                _FakeBlob(self._name, blob_name),
            )

    class _FakeStorageClient:
        def bucket(self, name: str) -> _FakeBucket:
            return _FakeBucket(name)

    monkeypatch.setattr(video_tools, "_get_gcs_client", lambda: _FakeStorageClient())
    monkeypatch.setattr(video_tools.config, "GCS_BUCKET_NAME", "allowed-bucket")

    ctx = SimpleNamespace()  # no ADK load_artifact -> exercises GCS fallback

    # 1. Cross-bucket and path-traversal URIs are rejected with zero GCS downloads
    for bad_uri in (
        "gs://evil-bucket/run/frame.png",
        "gs://allowed-bucket/run/../secret.png",
        "gs://allowed-bucket/./frame.png",
        "gs://allowed-bucket/run//frame.png",
        "gs://allowed-bucket/run\\frame.png",
        "gs://allowed-bucket/run\x00/frame.png",
    ):
        data, mime = asyncio.run(
            video_tools._load_keyframe_bytes(
                ctx, "Hero", {"Hero": {"gcs_uri": bad_uri}}
            )
        )
        assert data is None
        assert mime == "image/png"
    assert download_calls == []

    # 2. Oversized metadata (populated on reload) aborts BEFORE download_as_bytes()
    blobs_by_key[("allowed-bucket", "run/huge_meta.png")] = _FakeBlob(
        "allowed-bucket",
        "run/huge_meta.png",
        payload=b"tiny",
        reload_size=references.REFERENCE_MAX_BYTES + 1,
    )
    data_meta, _ = asyncio.run(
        video_tools._load_keyframe_bytes(
            ctx,
            "Hero",
            {"Hero": {"gcs_uri": "gs://allowed-bucket/run/huge_meta.png"}},
        )
    )
    assert data_meta is None
    assert download_calls == []

    # 3. Oversized payload (when metadata size was unknown) is rejected post-download
    blobs_by_key[("allowed-bucket", "run/huge_body.png")] = _FakeBlob(
        "allowed-bucket",
        "run/huge_body.png",
        payload=b"x" * (references.REFERENCE_MAX_BYTES + 1),
    )
    data_body, _ = asyncio.run(
        video_tools._load_keyframe_bytes(
            ctx,
            "Hero",
            {"Hero": {"gcs_uri": "gs://allowed-bucket/run/huge_body.png"}},
        )
    )
    assert data_body is None
    assert download_calls == [("allowed-bucket", "run/huge_body.png")]

    # 4. Unconfigured GCS_BUCKET_NAME skips GCS fallback completely
    download_calls.clear()
    monkeypatch.setattr(video_tools.config, "GCS_BUCKET_NAME", "")
    data_unconf, _ = asyncio.run(
        video_tools._load_keyframe_bytes(
            ctx,
            "Hero",
            {"Hero": {"gcs_uri": "gs://allowed-bucket/run/valid.jpg"}},
        )
    )
    assert data_unconf is None
    assert download_calls == []

    # 5. Valid confined URI downloads bytes and infers MIME type from extension
    monkeypatch.setattr(video_tools.config, "GCS_BUCKET_NAME", "allowed-bucket")
    blobs_by_key[("allowed-bucket", "run/valid.jpg")] = _FakeBlob(
        "allowed-bucket",
        "run/valid.jpg",
        payload=b"\xff\xd8\xffvalid-jpeg",
        reload_size=14,
    )
    data_ok, mime_ok = asyncio.run(
        video_tools._load_keyframe_bytes(
            ctx,
            "Hero",
            {"Hero": {"gcs_uri": "gs://allowed-bucket/run/valid.jpg"}},
        )
    )
    assert data_ok == b"\xff\xd8\xffvalid-jpeg"
    assert mime_ok == "image/jpeg"
