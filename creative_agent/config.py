import os
import warnings
from dataclasses import dataclass

from google.genai import errors as genai_errors
from pydantic import ValidationError

from agent_common.config import BaseAgentConfiguration
from agent_common.retry import build_infra_retry

warnings.filterwarnings("ignore")

# creative_agent calls genai directly (image gen), so it also retries the genai
# 5xx ServerError on top of the shared transient set.
INFRA_RETRY = build_infra_retry(extra_exceptions=[genai_errors.ServerError])

# Structured-output producers additionally retry a Pydantic ValidationError: at
# high temperature the model can emit invalid JSON (e.g. raw ESC control chars)
# that fails output_schema parsing, which crashed the run unretried under N=5
# concurrency (issue #104). ADK's RetryConfig matches on the exact exception class
# name, and pydantic raises `ValidationError`, so a bad sample is simply re-drawn.
# The infra set is included too (defense-in-depth for a 503 that escapes the genai
# HTTP-retry layer).
SCHEMA_RETRY = build_infra_retry(
    extra_exceptions=[genai_errors.ServerError, ValidationError]
)

# Arm C (DoE `global_altbucket`) model. Task 0a probe (2026-07-17) confirmed this
# is the one DISTINCT global gemini-3.x flash base model that both calls AND
# grounds via google_search @ global — a separate per-base-model quota bucket
# from the trend half's gemini-3.5-flash / gemini-3.1-flash-lite. Used for the
# campaign planner AND worker (no distinct global flash-lite exists).
ALT_GLOBAL_MODEL = "gemini-3-flash-preview"


@dataclass
class ResearchConfiguration(BaseAgentConfiguration):
    """Research config for creative_agent.

    Quota spread (mirrors trend_scout PR #94): the one ``ParallelAgent`` in the
    tree, ``parallel_planner_agent``, runs the trend- and campaign-research
    pipelines at the same time. Both used to drive off the same global buckets,
    so each step fired *two* concurrent calls into one ~5-RPM pool
    (``gemini-3.1-flash-lite`` planners, then ``gemini-3.5-flash`` searchers +
    synthesizers) → ``429 RESOURCE_EXHAUSTED``. Vertex quota is **per-base-model**
    (and, off ``global``, **per-region**), so we pin the *campaign* half to a
    separate regional pool:

    - trend planner/searcher/synthesizer  → base ``lite_planner_model`` /
      ``worker_model`` (gemini-3.x)                                @ global
    - campaign planner                    → ``regional_lite_planner_model``
      (gemini-2.5-flash-lite)                                      @ us-central1
    - campaign searcher + synthesizer     → ``regional_worker_model``
      (gemini-2.5-flash)                                           @ us-central1

    This drops each global family's bucket from 2 concurrent callers to 1 during
    the parallel phase. gemini-2.5 models 404 on ``global`` and must run
    regionally (the inverse of gemini-3.x); sibling/alias names
    (``gemini-flash-early-exp*``) 404, so these must be genuine base models.
    The eval judge (gemini-3.1-pro-preview @ global) is deliberately left on its
    bucket — a prior A/B rejected gemini-2.5-pro there (grades softer).
    """

    regional_model_location: str = "us-central1"
    regional_worker_model: str = "gemini-2.5-flash"  # campaign searcher + synthesizer
    regional_lite_planner_model: str = "gemini-2.5-flash-lite"  # campaign planner

    # DoE arm selector (2026-07-17): one codebase deploys as three model-placement
    # arms differing ONLY by this env var, so isolated Cloud Run revisions can
    # quantify the PR #101 quota-bucket spread. Default `regional_25` preserves
    # the shipped #101 behavior exactly, so prod is untouched unless set.
    campaign_research_placement: str = os.environ.get(
        "CAMPAIGN_RESEARCH_PLACEMENT", "regional_25"
    )

    def campaign_models(self) -> tuple[str, str, str]:
        """(lite_planner_model, worker_model, location) for the campaign pipeline.

        Resolves the campaign-research half's models per DoE arm:
        - ``regional_25`` (default): gemini-2.5 @ us-central1 — the shipped #101 spread.
        - ``global_3x`` (Arm A): shares the trend half's global 3.x buckets (baseline
          double-up that #101 moved away from).
        - ``global_altbucket`` (Arm C): a distinct global 3.x bucket (Task 0a) — same
          region+family as the trend half, only the base-model bucket differs.

        An unrecognized value degrades to ``regional_25`` (the safe default).
        """
        arms = {
            "regional_25": (
                self.regional_lite_planner_model,
                self.regional_worker_model,
                self.regional_model_location,
            ),
            "global_3x": (self.lite_planner_model, self.worker_model, "global"),
            "global_altbucket": (ALT_GLOBAL_MODEL, ALT_GLOBAL_MODEL, "global"),
        }
        return arms.get(self.campaign_research_placement, arms["regional_25"])


config = ResearchConfiguration()
