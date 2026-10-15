"""Gemini sampling profile: today's tuned per-agent temperatures, or the Gemini 3 default (1.0).

Gemini 3 docs recommend keeping ``temperature`` at its default of 1.0 (lower
values may cause looping or degraded output):
https://ai.google.dev/gemini-api/docs/gemini-3. ``GEMINI_TEMPERATURE_PROFILE``
selects ``tuned`` (default: every agent keeps its hand-tuned value) or
``default`` (every agent with an explicit temperature gets 1.0), so the two can
be A/B tested as revisions that differ only in this env var.

Read once at import: Agent Engine pickles the agents (with their
``GenerateContentConfig``) using the deployer's env; Cloud Run reads it at
revision start. Not used by the ``creative_eval`` judge or image QA. ADK-free.
"""

import logging
import os

PROFILES = ("tuned", "default")
DEFAULT_PROFILE = "tuned"
GEMINI3_DEFAULT_TEMPERATURE = 1.0

logger = logging.getLogger(__name__)


def _profile() -> str:
    """Parse ``GEMINI_TEMPERATURE_PROFILE``; blank → tuned, unknown → tuned + warning."""
    raw = (os.getenv("GEMINI_TEMPERATURE_PROFILE") or "").strip().lower()
    if not raw:
        return DEFAULT_PROFILE
    if raw not in PROFILES:
        logger.warning(
            "Unknown GEMINI_TEMPERATURE_PROFILE %r; falling back to %r",
            raw,
            DEFAULT_PROFILE,
        )
        return DEFAULT_PROFILE
    return raw


PROFILE = _profile()


def temperature(tuned: float) -> float:
    """The agent's sampling temperature under the active profile."""
    return tuned if PROFILE == "tuned" else GEMINI3_DEFAULT_TEMPERATURE
