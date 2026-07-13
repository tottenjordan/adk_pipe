"""Shared building blocks used across the agent packages."""

from agent_common.locations import MODEL_LOCATION
from agent_common.models import build_gemini

__all__ = ["MODEL_LOCATION", "build_gemini"]
