"""Tests for the scoped RetryConfig constants attached to infra-calling agents."""


class TestTrendTrawlerRetryConfig:
    def test_infra_retry_scoped_and_bounded(self):
        from trend_trawler.config import INFRA_RETRY

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
        # trend_trawler has no direct genai calls
        assert "ServerError" not in names


class TestCreativeAgentRetryConfig:
    def test_infra_retry_includes_genai_server_error(self):
        from creative_agent.config import INFRA_RETRY

        assert INFRA_RETRY.max_attempts == 3
        names = set(INFRA_RETRY.exceptions)
        assert "ServerError" in names           # genai 5xx
        assert "ServiceUnavailable" in names     # api_core
        assert "TimeoutError" in names


class TestAgentsHaveRetryConfig:
    def test_trend_trawler_agents_have_retry(self):
        from trend_trawler.agent import gather_trends_agent, trend_trawler
        from trend_trawler.config import INFRA_RETRY

        assert gather_trends_agent.retry_config is INFRA_RETRY
        assert trend_trawler.retry_config is INFRA_RETRY

    def test_creative_agent_agents_have_retry(self):
        from creative_agent.agent import visual_generator, root_agent
        from creative_agent.config import INFRA_RETRY

        assert visual_generator.retry_config is INFRA_RETRY
        assert root_agent.retry_config is INFRA_RETRY
