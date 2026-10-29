"""The run's gate policy, read once from session state (pure, no ADK imports).

The deterministic gates (``brief_check.check_brief``, ``copy_gate.gate_copies``
and the concept checks) depend on the same handful of run facts: the campaign
inputs, the timely slot, the rating strictness flags, the sensitivity / safer
angle switches and the brief's term lists. ``RunPolicy.from_state`` reads them
from a state snapshot in exactly one place, so the gates take one ``policy``
argument instead of 10-17 loose keyword arguments threaded through several
layers.

Every field has a neutral default, so ``RunPolicy()`` (or one with only the
fields a unit test cares about) is the "nothing configured" policy the gates
had with their old keyword defaults.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import safer_angle
from .claims import claims_allowed_text
from .copy_gate import (
    brief_avoid,
    brief_mandatories,
    brief_motifs,
    brief_risks,
    brief_safer_angle,
)
from .rating_signals import strictness_flags
from .references import resolve_references
from .trend_situation import timely_allowed


class _TimelyConfig(Protocol):
    """The config knob ``from_state`` reads (``creative_agent.config.config``)."""

    @property
    def max_timely_concepts(self) -> int: ...


def selling_points(state: Mapping[str, Any]) -> str | list[str]:
    """The user's ``key_selling_points`` (a string or a list of strings)."""
    points = state.get("key_selling_points")
    if isinstance(points, list):
        return [p for p in points if isinstance(p, str)]
    return str(points or "")


def is_trend_sensitive(state: Mapping[str, Any]) -> bool:
    """The trend must not be referenced: the validated current moment is
    sensitive, or trend_scout flagged the trend (``trend_sensitive_hint``),
    which covers a creative run whose own situation search missed it."""
    situation = state.get("trend_situation")
    return (
        isinstance(situation, Mapping) and situation.get("phase") == "sensitive"
    ) or safer_angle.is_flagged_sensitive(state)


@dataclass(frozen=True)
class RunPolicy:
    """What the deterministic gates need to know about the run.

    * ``brand`` / ``target_product`` / ``trend`` — the campaign inputs
      (``target_search_trends`` for the trend);
    * ``timely_ok`` / ``max_timely`` — the timely slot
      (``trend_situation.timely_allowed`` and ``MAX_TIMELY_CONCEPTS``);
    * ``strictness`` — the known ``rating_strictness`` flags (only tighten);
    * ``trend_sensitive`` — the current moment is sensitive or trend_scout
      flagged the trend (the trend tie is skipped);
    * ``safer_angle_active`` / ``safer_angle_text`` — the opt-in safer angle
      and the brief's ``trend_bridge.safer_angle``;
    * ``claims_allowed_text`` — the user's selling points, the only text that
      licenses an absolute claim;
    * ``avoid`` / ``mandatories`` / ``motifs`` / ``risks`` — the brief's term
      lists (``avoid`` raw: the copy gate filters it per check);
    * ``brand_colors`` / ``sources`` / ``trend_situation`` — the brief check's
      extra inputs (``sources`` None = not checked).
    """

    brand: str = ""
    target_product: str = ""
    trend: str = ""
    timely_ok: bool = False
    max_timely: int = 1
    strictness: Sequence[str] = ()
    trend_sensitive: bool = False
    safer_angle_active: bool = False
    safer_angle_text: str = ""
    claims_allowed_text: str = ""
    avoid: Sequence[str] | str = ()
    mandatories: Sequence[str] = ()
    motifs: Sequence[str] = ()
    risks: Sequence[str] = ()
    brand_colors: str = ""
    reference_roles: Sequence[str] | str = ()
    visual_style_preference: str = ""
    sources: Mapping[str, Any] | None = None
    enterprise_sources: Mapping[str, Any] | None = None
    trend_situation: Mapping[str, Any] | None = None

    @classmethod
    def from_state(cls, state: Mapping[str, Any], config: _TimelyConfig) -> RunPolicy:
        """The policy for a state snapshot (a Mapping; never raises)."""
        brief = state.get("creative_brief")
        sources = state.get("sources")
        ent_sources = state.get("enterprise_sources")
        situation = state.get("trend_situation")
        max_timely = config.max_timely_concepts
        raw_roles = state.get("reference_roles")
        roles: Sequence[str] | str = (
            raw_roles
            if raw_roles
            else tuple(role for _, role in resolve_references(state))
        )
        return cls(
            brand=str(state.get("brand") or ""),
            target_product=str(state.get("target_product") or ""),
            trend=str(state.get("target_search_trends") or ""),
            timely_ok=timely_allowed(state, max_timely),
            max_timely=max_timely,
            strictness=strictness_flags(state.get("rating_strictness")),
            trend_sensitive=is_trend_sensitive(state),
            safer_angle_active=safer_angle.is_active(state),
            safer_angle_text=brief_safer_angle(brief),
            # Only the user's selling points license a claim (never the
            # brief's mandatories, which the brief writer writes).
            claims_allowed_text=claims_allowed_text(selling_points(state)),
            avoid=tuple(brief_avoid(brief)),
            mandatories=tuple(brief_mandatories(brief)),
            motifs=tuple(brief_motifs(brief)),
            risks=tuple(brief_risks(brief)),
            brand_colors=str(state.get("brand_colors") or ""),
            reference_roles=roles,
            visual_style_preference=str(state.get("visual_style_preference") or ""),
            sources=sources if isinstance(sources, Mapping) else None,
            enterprise_sources=(
                ent_sources if isinstance(ent_sources, Mapping) else None
            ),
            trend_situation=situation if isinstance(situation, Mapping) else None,
        )
