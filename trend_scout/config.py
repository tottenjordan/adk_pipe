import warnings
from dataclasses import dataclass

from agent_common.config import BaseAgentConfiguration
from agent_common.retry import build_infra_retry

warnings.filterwarnings("ignore")


# Shared transient-error retry (no direct genai calls here, so no ServerError).
INFRA_RETRY = build_infra_retry()


@dataclass
class ResearchConfiguration(BaseAgentConfiguration):
    """Research config for trend_scout — all fields shared via the base."""


config = ResearchConfiguration()
