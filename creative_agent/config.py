import warnings
from dataclasses import dataclass

from google.genai import errors as genai_errors

from agent_common.config import BaseAgentConfiguration
from agent_common.retry import build_infra_retry

warnings.filterwarnings("ignore")

# creative_agent calls genai directly (image gen), so it also retries the genai
# 5xx ServerError on top of the shared transient set.
INFRA_RETRY = build_infra_retry(extra_exceptions=[genai_errors.ServerError])


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


config = ResearchConfiguration()
