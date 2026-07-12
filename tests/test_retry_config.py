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
