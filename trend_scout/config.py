from dataclasses import dataclass

from agent_common.config import BaseAgentConfiguration
from agent_common.retry import build_infra_retry

# Shared transient-error retry (no direct genai calls here, so no ServerError).
INFRA_RETRY = build_infra_retry()


@dataclass
class ResearchConfiguration(BaseAgentConfiguration):
    """Research config for trend_scout.

    Quota spread: all 5 trend_scout agents used to drive off ``worker_model``,
    funneling into the single ``gemini-3.5-flash`` default quota bucket (5 RPM,
    project-wide/shared). One UI run overshoots it →
    ``429 RESOURCE_EXHAUSTED``, and waiting doesn't help (the 5/min is shared and
    a single run bursts past it). Vertex quota is **per-base-model** (and, off
    ``global``, **per-region**), so we fan the 5 agents across five separate
    buckets instead of one:

    - searcher      → ``worker_model``       gemini-3.8-flash        @ global
    - synthesizer   → ``lite_planner_model`` gemini-3.5-flash-lite   @ global
    - root          → ``critic_model``       gemini-3.1-pro-preview  @ global
    - gather        → ``gather_model``       gemini-3.1-flash-lite   @ global
    - pick          → ``picker_model``       gemini-3.5-flash        @ global

    All five are distinct gemini-3.x base models, so each still owns its own
    per-base-model bucket. Until 2026-09 gather/pick ran on gemini-2.5-flash-lite /
    gemini-2.5-pro @ us-central1 (the regional pool); retired because Vertex
    blocks/shuts down gemini-2.5. gather is trivial tool-output formatting, so the
    previous-gen flash-lite suffices; pick (the 25→3 judgment) gets a full flash
    model rather than the critic's pro bucket, which the root orchestrator
    already drives. creative_agent's campaign half also uses gemini-3.5-flash,
    but the two agents don't run inside one ParallelAgent together.
    """

    gather_model: str = "gemini-3.1-flash-lite"
    picker_model: str = "gemini-3.5-flash"


config = ResearchConfiguration()
