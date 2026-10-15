"""Tests for the scoped RetryConfig constants attached to infra-calling agents."""


class TestTrendTrawlerRetryConfig:
    def test_infra_retry_scoped_and_bounded(self):
        from trend_scout.config import INFRA_RETRY

        assert INFRA_RETRY.max_attempts == 3
        # exceptions are stored as class-name strings by RetryConfig's validator
        names = set(INFRA_RETRY.exceptions)
        assert {
            "ServiceUnavailable",
            "InternalServerError",
            "GatewayTimeout",
            "TooManyRequests",
            "DeadlineExceeded",
            "ConnectionError",
            "TimeoutError",
        } <= names
        # ADK's Gemini model raises genai errors, so the genai 5xx must be listed
        assert "ServerError" in names

    def test_model_server_error_is_retried(self):
        """A genai 5xx from a trend_scout model call makes ADK retry the node."""
        from google.adk.workflow._node_state import NodeState
        from google.adk.workflow.utils._retry_utils import _should_retry_node
        from google.genai import errors as genai_errors

        from trend_scout.config import INFRA_RETRY

        exc = genai_errors.ServerError(503, {"error": {"message": "unavailable"}})
        assert _should_retry_node(exc, INFRA_RETRY, NodeState())


class TestRetryMatchesSubclasses:
    """ADK 2.10 matches RetryConfig names against the exception's MRO."""

    def test_subclass_of_listed_class_is_retried(self):
        from google.adk.workflow._node_state import NodeState
        from google.adk.workflow.utils._retry_utils import _should_retry_node

        from agent_common.retry import build_infra_retry

        class SlowConnection(ConnectionError):
            pass

        assert _should_retry_node(SlowConnection(), build_infra_retry(), NodeState())
        assert not _should_retry_node(ValueError(), build_infra_retry(), NodeState())


class TestCreativeAgentRetryConfig:
    def test_infra_retry_includes_genai_server_error(self):
        from creative_agent.config import INFRA_RETRY

        assert INFRA_RETRY.max_attempts == 3
        names = set(INFRA_RETRY.exceptions)
        assert "ServerError" in names  # genai 5xx
        assert "ServiceUnavailable" in names  # api_core
        assert "TimeoutError" in names


class TestAgentsHaveRetryConfig:
    def test_trend_scout_agents_have_retry(self):
        from trend_scout.agent import gather_trends_agent, trend_scout
        from trend_scout.config import INFRA_RETRY

        assert gather_trends_agent.retry_config is INFRA_RETRY
        assert trend_scout.retry_config is INFRA_RETRY

    def test_creative_agent_agents_have_retry(self):
        from creative_agent.agent import root_agent, visual_generator_resilient
        from creative_agent.config import INFRA_RETRY

        # The render node (no LLM) retries transient infra errors like the
        # visual_generator agent it replaced.
        assert visual_generator_resilient.retry_config is INFRA_RETRY
        assert root_agent.retry_config is INFRA_RETRY

    def test_interactive_creative_agent_has_retry(self):
        from creative_agent.config import INFRA_RETRY
        from interactive_creative.agent import root_agent

        assert root_agent.retry_config is INFRA_RETRY
