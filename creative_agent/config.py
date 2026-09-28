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

# Campaign-research model for the default `global_altbucket` arm. 2026-09
# lineup refresh: gemini-3.5-flash (stable, previous-gen flash) is a DISTINCT
# per-base-model quota bucket from both trend-half models (gemini-3.8-flash
# worker / gemini-3.5-flash-lite planner) and was probed to call AND ground via
# google_search @ global. Used for the campaign planner AND worker. (Previously
# gemini-3-flash-preview, when this was only the DoE Arm C.)
ALT_GLOBAL_MODEL = "gemini-3.5-flash"

DEFAULT_CAMPAIGN_ARM = "global_altbucket"


@dataclass
class ResearchConfiguration(BaseAgentConfiguration):
    """Research config for creative_agent.

    Quota spread (mirrors trend_scout PR #94, shipped as PR #101): the one
    ``ParallelAgent`` in the tree, ``parallel_planner_agent``, runs the trend- and
    campaign-research pipelines at the same time. If both drive off the same
    buckets, each step fires *two* concurrent calls into one small pool →
    ``429 RESOURCE_EXHAUSTED``. Vertex quota is **per-base-model**, so the
    *campaign* half runs on a different base model from the trend half:

    - trend planner/searcher/synthesizer  → base ``lite_planner_model`` /
      ``worker_model`` (gemini-3.5-flash-lite / gemini-3.8-flash)   @ global
    - campaign planner/searcher/synthesizer → ``ALT_GLOBAL_MODEL``
      (gemini-3.5-flash)                                            @ global

    This keeps each base-model bucket at 1 concurrent caller during the parallel
    phase. (Until 2026-09 the campaign half ran on gemini-2.5 @ us-central1 — the
    ``regional_25`` arm — retired because Vertex blocks/shuts down gemini-2.5.)
    Sibling/alias names (``gemini-flash-early-exp*``) 404, so these must be
    genuine base models. The eval judge (gemini-3.1-pro-preview @ global) is
    deliberately left on its bucket — a prior A/B rejected gemini-2.5-pro there.
    """

    # Placement arm selector. Originally the quota-spread DoE seam (2026-07-17);
    # now just selects the campaign half's bucket. Default `global_altbucket`.
    campaign_research_placement: str = os.environ.get(
        "CAMPAIGN_RESEARCH_PLACEMENT", DEFAULT_CAMPAIGN_ARM
    )

    def campaign_models(self) -> tuple[str, str, str]:
        """(lite_planner_model, worker_model, location) for the campaign pipeline.

        Resolves the campaign-research half's models per placement arm:
        - ``global_altbucket`` (default): ``ALT_GLOBAL_MODEL`` @ global — a
          distinct base-model bucket from the trend half.
        - ``global_3x``: shares the trend half's global buckets (the double-up
          baseline #101 moved away from; kept for comparison runs).

        An unrecognized value (incl. the retired ``regional_25``) degrades to
        the default ``global_altbucket``.
        """
        arms = {
            "global_3x": (self.lite_planner_model, self.worker_model, "global"),
            "global_altbucket": (ALT_GLOBAL_MODEL, ALT_GLOBAL_MODEL, "global"),
        }
        return arms.get(self.campaign_research_placement, arms[DEFAULT_CAMPAIGN_ARM])


config = ResearchConfiguration()
