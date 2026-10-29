"""Opt-in Phase 3 multi-cut commercial video production stage.

Activated per run via session ``state["video_production_enabled"]`` (default
``False``). Split into two reusable ``PipelineRequest`` Workflows —
``video_storyboard_pipeline`` and ``video_render_pipeline`` — so
``interactive_creative`` can pause at Checkpoint 4 (``review_video_storyboard``)
between storyboard planning and multi-cut video rendering while
``creative_pipeline`` chains both behind ``video_gate`` (0 ms skip when
``video_production_enabled`` is ``False``).
"""

import copy
import logging
from collections.abc import Mapping
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.planners import BuiltInPlanner
from google.adk.workflow import Workflow
from google.genai import types

from agent_common import (
    FailSoftNode,
    PipelineRequest,
    RetryUntilKeyNode,
    build_gemini,
    is_populated,
)
from agent_common.sampling import temperature as sampled_temperature

from . import callbacks, prompts, video_qa, video_tools
from .config import SCHEMA_RETRY, config, is_video_production_enabled
from .finalize import issue_message
from .research_stage import missing_notice
from .schemas import VideoStoryboard

video_director = Agent(
    model=build_gemini(config.worker_model),
    name="video_director",
    mode="single_turn",
    include_contents="none",
    description="Plans a 30s hero + 15s social + 6s bumper commercial storyboard anchored on Phase 2 keyframes.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.VIDEO_DIRECTOR_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=sampled_temperature(0.6),
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "video_director",
        },
    ),
    output_schema=VideoStoryboard,
    retry_config=SCHEMA_RETRY,
    output_key="video_storyboard",
    before_agent_callback=callbacks.reset_video_state,
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=[
        callbacks.scrub_surrogates_in_response,
        callbacks.log_empty_turn_finish_reason,
    ],
)

video_director_resilient = RetryUntilKeyNode(
    name="video_director_resilient",
    node=video_director,
    output_key="video_storyboard",
    max_attempts=2,
)


def _video_director_failed(state: Mapping[str, Any], exc: Exception) -> dict[str, Any]:
    del state
    return {
        "video_storyboard__retry_exhausted": True,
        "video_storyboard__issues": [issue_message("video storyboard", exc)],
    }


video_director_failsoft = FailSoftNode(
    name="video_director_failsoft",
    node=video_director_resilient,
    on_error=_video_director_failed,
)

video_storyboard_reviser = Agent(
    model=build_gemini(config.worker_model),
    name="video_storyboard_reviser",
    mode="single_turn",
    include_contents="none",
    description="Revises the Phase 3 video storyboard to clear deterministic duration, voiceover, and super issues.",
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=False)
    ),
    instruction=prompts.VIDEO_DIRECTOR_INSTR,
    generate_content_config=types.GenerateContentConfig(
        temperature=sampled_temperature(0.4),
        labels={
            "agentic_wf": "trend_scout",
            "agent": "creative_agent",
            "subagent": "video_storyboard_reviser",
        },
    ),
    output_schema=VideoStoryboard,
    retry_config=SCHEMA_RETRY,
    output_key="video_storyboard",
    before_model_callback=callbacks.rate_limit_callback,
    after_model_callback=[
        callbacks.scrub_surrogates_in_response,
        callbacks.log_empty_turn_finish_reason,
    ],
)


def _video_storyboard_reviser_failed(
    state: Mapping[str, Any], exc: Exception
) -> dict[str, Any]:
    storyboard = (
        state.get("video_storyboard__before_revision")
        if is_populated(state.get("video_storyboard__before_revision"))
        else state.get("video_storyboard")
    )
    used = int(state.get("video_storyboard_revision_rounds_used") or 0)
    issues = (
        video_qa.storyboard_issues(storyboard, state)
        if is_populated(storyboard)
        else [issue_message("video storyboard reviser", exc)]
    )
    delta: dict[str, Any] = {
        "video_storyboard_issues": "",
        "video_storyboard__before_revision": None,
        "video_storyboard__issues": issues or None,
        "video_storyboard_revision_rounds_used": max(
            used, config.storyboard_revision_rounds
        ),
    }
    if is_populated(state.get("video_storyboard__before_revision")):
        delta["video_storyboard"] = copy.deepcopy(
            state["video_storyboard__before_revision"]
        )
    return delta


video_storyboard_reviser_failsoft = FailSoftNode(
    name="video_storyboard_reviser_failsoft",
    node=video_storyboard_reviser,
    on_error=_video_storyboard_reviser_failed,
)


def storyboard_gate_decision(
    state: Mapping[str, Any], max_rounds: int
) -> tuple[str, dict[str, Any]]:
    """Pure (route, state_delta) decision for ``storyboard_gate``."""
    storyboard = state.get("video_storyboard")
    if not is_populated(storyboard):
        return "ok", {
            "video_storyboard_issues": "",
            "video_storyboard__before_revision": None,
        }

    issues = video_qa.storyboard_issues(storyboard, state)
    if not issues:
        return "ok", {
            "video_storyboard_issues": "",
            "video_storyboard__before_revision": None,
            "video_storyboard__issues": None,
        }

    used = int(state.get("video_storyboard_revision_rounds_used") or 0)
    if used < max_rounds:
        formatted = "\n".join(f"- {item}" for item in issues)
        return "revise", {
            "video_storyboard_issues": formatted,
            "video_storyboard__before_revision": copy.deepcopy(storyboard),
            "video_storyboard_revision_rounds_used": used + 1,
        }

    return "ok", {
        "video_storyboard_issues": "",
        "video_storyboard__before_revision": None,
        "video_storyboard__issues": issues,
    }


def storyboard_gate(ctx: Context) -> Event:
    """Route a storyboard with deterministic issues to the reviser while budget remains."""
    route, delta = storyboard_gate_decision(
        ctx.state.to_dict(), config.storyboard_revision_rounds
    )
    if delta.get("video_storyboard__issues"):
        logging.warning(
            "video storyboard issues remain: %s", delta["video_storyboard__issues"]
        )
    return Event(actions=EventActions(route=route, state_delta=delta))


def video_storyboard_enabled_gate(ctx: Context) -> Event:
    """Zero-cost entry guard on ``video_storyboard_pipeline`` when disabled."""
    route = (
        "run"
        if is_video_production_enabled(ctx.state.get("video_production_enabled"))
        else "skip"
    )
    return Event(actions=EventActions(route=route))


def video_storyboard_skipped(ctx: Context) -> str:
    """Truthy terminal when ``video_storyboard_pipeline`` is called with video disabled."""
    del ctx
    return "Video production is disabled (video_production_enabled is False); storyboard skipped."


def video_storyboard_ready(ctx: Context) -> str:
    """Truthy terminal of ``video_storyboard_pipeline``."""
    value = ctx.state.get("video_storyboard")
    if is_populated(value):
        parsed = video_qa.parse_storyboard(value) or {}
        shots = parsed.get("shots")
        count = len(shots) if isinstance(shots, list) else 0
        return (
            f"Video storyboard complete: {count} shot(s) saved to session state "
            "as 'video_storyboard'."
        )
    return missing_notice("video_director", "video_storyboard")


video_storyboard_pipeline = Workflow(
    name="video_storyboard_pipeline",
    description="Plans and validates a multi-cut commercial video storyboard (30s hero, 15s social, 6s bumper) when video_production_enabled is True.",
    input_schema=PipelineRequest,
    edges=[
        ("START", video_storyboard_enabled_gate),
        (
            video_storyboard_enabled_gate,
            {
                "skip": video_storyboard_skipped,
                "run": video_director_failsoft,
            },
        ),
        (video_director_failsoft, storyboard_gate),
        (
            storyboard_gate,
            {
                "ok": video_storyboard_ready,
                "revise": video_storyboard_reviser_failsoft,
            },
        ),
        (video_storyboard_reviser_failsoft, storyboard_gate),
    ],
)


async def produce_video_reel_node(ctx: Context) -> None:
    """Fail-soft function node rendering shots, QA/retakes, audio, and FFmpeg cuts."""
    if not is_video_production_enabled(ctx.state.get("video_production_enabled")):
        return None
    try:
        await video_tools.produce_video_reel(ctx)
    except Exception as exc:
        logging.warning("produce_video_reel_node failed (fail-soft): %s", exc)
        ctx.state["generated_videos__issues"] = [issue_message("video reel", exc)]
    return None


def videos_ready(ctx: Context) -> str:
    """Truthy terminal of ``video_render_pipeline``."""
    if not is_video_production_enabled(ctx.state.get("video_production_enabled")):
        return "Video production is disabled (video_production_enabled is False); video rendering skipped."
    videos = ctx.state.get("generated_videos")
    if isinstance(videos, Mapping) and videos:
        cuts = ", ".join(str(k) for k in videos)
        return (
            f"Video reel rendered: {len(videos)} cut(s) ({cuts}) saved to session "
            "state as 'generated_videos'."
        )
    return missing_notice("produce_video_reel_node", "generated_videos")


video_render_pipeline = Workflow(
    name="video_render_pipeline",
    description="Renders storyboard video shots with multimodal QA, voiceover/music synthesis, and FFmpeg multi-cut conform (30s 16:9, 15s 9:16, 6s 9:16).",
    input_schema=PipelineRequest,
    edges=[("START", produce_video_reel_node, videos_ready)],
)


def video_gate(ctx: Context) -> Event:
    """Route past Phase 3 video production in 0 ms unless ``video_production_enabled`` is True."""
    route = (
        "produce"
        if is_video_production_enabled(ctx.state.get("video_production_enabled"))
        else "skip"
    )
    return Event(actions=EventActions(route=route))
