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
    """Research config for creative_agent — all fields shared via the base."""


config = ResearchConfiguration()
