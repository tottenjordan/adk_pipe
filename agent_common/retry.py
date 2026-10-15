"""Factory for the shared ADK infra-retry config.

``RetryConfig.exceptions`` is stored as class-name strings. ADK 2.10 retries a
node when any class in the raised exception's MRO (``object`` excluded) has a
listed name (``google/adk/workflow/utils/_retry_utils.py`` ``_should_retry_node``),
so a listed class also covers its subclasses. We still enumerate the concrete
transient classes rather than a broad base like ``GoogleAPICallError``, which
would also retry permanent 4xx errors.

Every agent's models raise genai errors (ADK's ``Gemini`` uses google-genai), so
agents pass the genai ``ServerError`` as an extra.
``build_infra_retry(extra_exceptions=...)`` is the one place the base list is
defined; callers pass their extras.
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
