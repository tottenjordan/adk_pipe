"""Factory for the shared ADK infra-retry config.

ADK 2.0 retries a node when the raised exception's EXACT class name is in the
``RetryConfig.exceptions`` list (it does NOT use ``isinstance``), so we enumerate
the concrete transient classes Google clients actually raise — base classes like
``GoogleAPICallError`` never match.

``trend_trawler`` and ``creative_agent`` shared this list verbatim except that
creative_agent also calls genai directly (image gen) and so must retry the genai
``ServerError``. ``build_infra_retry(extra_exceptions=...)`` is the one place that
list is defined; callers pass their extras.
"""

from google.adk.workflow import RetryConfig
from google.api_core import exceptions as api_exceptions

# Concrete transient exception classes common to every agent.
_BASE_INFRA_EXCEPTIONS = (
    api_exceptions.ServiceUnavailable,  # 503
    api_exceptions.InternalServerError,  # 500
    api_exceptions.GatewayTimeout,  # 504
    api_exceptions.TooManyRequests,  # 429
    api_exceptions.DeadlineExceeded,
    ConnectionError,
    TimeoutError,
)


def build_infra_retry(extra_exceptions=(), max_attempts: int = 3) -> RetryConfig:
    """Return a ``RetryConfig`` covering the base transient exceptions plus extras.

    Args:
        extra_exceptions: additional concrete exception classes to retry (e.g.
            the genai ``ServerError`` for agents that call genai directly).
        max_attempts: total attempts before giving up.
    """
    return RetryConfig(
        max_attempts=max_attempts,
        exceptions=[*_BASE_INFRA_EXCEPTIONS, *extra_exceptions],
    )
