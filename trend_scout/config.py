import warnings
from dataclasses import dataclass

from agent_common.config import BaseAgentConfiguration
from agent_common.retry import build_infra_retry

warnings.filterwarnings("ignore")


# Shared transient-error retry (no direct genai calls here, so no ServerError).
INFRA_RETRY = build_infra_retry()


@dataclass
class ResearchConfiguration(BaseAgentConfiguration):
    """Research config for trend_scout.

    Quota spread: all 5 trend_scout agents used to drive off ``worker_model``,
    funneling into the single ``gemini-3.5-flash`` default quota bucket (5 RPM,
    project-wide/shared on ``hybrid-vertex``). One UI run overshoots it →
    ``429 RESOURCE_EXHAUSTED``, and waiting doesn't help (the 5/min is shared and
    a single run bursts past it). Vertex quota is **per-base-model** (and, off
    ``global``, **per-region**), so we fan the 5 agents across five separate
    buckets instead of one:

    - searcher      → ``worker_model``       gemini-3.5-flash        @ global
    - synthesizer   → ``lite_planner_model`` gemini-3.1-flash-lite   @ global
    - root          → ``critic_model``       gemini-3.1-pro-preview  @ global
    - gather        → ``gather_model``       gemini-2.5-flash-lite   @ us-central1
    - pick          → ``picker_model``       gemini-2.5-pro          @ us-central1

    The two gemini-2.5 agents run at ``regional_model_location`` (us-central1),
    landing in the *regional* per-base-model quota — a pool entirely separate
    from the ``global`` buckets the gemini-3.x agents use, and unused elsewhere
    in the pipeline. ``pick`` on gemini-2.5-pro is also a deliberate quality
    upgrade for the 25→3 judgment step. creative_agent is untouched.
    """

    # gemini-2.5 models are served regionally; global 404s them.
    regional_model_location: str = "us-central1"
    gather_model: str = "gemini-2.5-flash-lite"
    picker_model: str = "gemini-2.5-pro"


config = ResearchConfiguration()
