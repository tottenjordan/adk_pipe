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
        # trend_scout has no direct genai calls
        assert "ServerError" not in names


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
        from creative_agent.agent import visual_generator, root_agent
        from creative_agent.config import INFRA_RETRY

        assert visual_generator.retry_config is INFRA_RETRY
        assert root_agent.retry_config is INFRA_RETRY

    def test_interactive_creative_agent_has_retry(self):
        from interactive_creative.agent import root_agent
        from creative_agent.config import INFRA_RETRY

        assert root_agent.retry_config is INFRA_RETRY
