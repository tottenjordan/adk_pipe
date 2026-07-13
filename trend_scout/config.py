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


@dataclass
class SetupConfiguration:
    """Configuration for general setup

    Attributes:
        state_init (str): a key indicating the state dict is initialized
        empty_session_state (dict): Empty dictionary with keys for initial ADK session state.

    """

    state_init = "_state_init"
    empty_session_state = {
        "state": {
            "brand": "",
            "target_product": "",
            "target_audience": "",
            "key_selling_points": "",
            "target_search_trends": {"target_search_trends": []},
            "target_yt_trends": {"target_yt_trends": []},
        }
    }


setup_config = SetupConfiguration()
