"""Shared building blocks used across the agent packages."""

from agent_common.clients import get_bigquery_client, get_gcs_client
from agent_common.config import BaseAgentConfiguration
from agent_common.fail_soft_node import FailSoftNode
from agent_common.history import drop_other_agent_context
from agent_common.idempotency import stable_row_id
from agent_common.locations import MODEL_LOCATION
from agent_common.models import (
    ROOT_EMPTY_TURN_RETRIES,
    build_gemini,
    build_gemini_with_fallback,
)
from agent_common.observability import (
    collect_degradation_warnings,
    log_empty_turn_finish_reason,
    log_run_start,
    make_final_state_summary,
)
from agent_common.rate_limit import build_rate_limit_callback
from agent_common.retry import build_infra_retry
from agent_common.retry_node import RetryUntilKeyNode, is_populated
from agent_common.safety import ScopedModelArmorPlugin, build_safety_plugins
from agent_common.sanitize import (
    scrub_lone_surrogates,
    scrub_surrogates_in_response,
)
from agent_common.schemas import PipelineRequest
from agent_common.state import memorize, seed_initial_state

__all__ = [
    "BaseAgentConfiguration",
    "FailSoftNode",
    "MODEL_LOCATION",
    "PipelineRequest",
    "ROOT_EMPTY_TURN_RETRIES",
    "build_gemini",
    "build_gemini_with_fallback",
    "build_infra_retry",
    "build_rate_limit_callback",
    "build_safety_plugins",
    "RetryUntilKeyNode",
    "ScopedModelArmorPlugin",
    "collect_degradation_warnings",
    "drop_other_agent_context",
    "get_bigquery_client",
    "get_gcs_client",
    "is_populated",
    "log_empty_turn_finish_reason",
    "log_run_start",
    "make_final_state_summary",
    "memorize",
    "scrub_lone_surrogates",
    "scrub_surrogates_in_response",
    "seed_initial_state",
    "stable_row_id",
]
