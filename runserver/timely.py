"""Timely creatives and their expiry (pure; the server-side share/experiment guards).

A creative is **timely** when its visual concept is flagged ``uses_trend_state``
or its paired ad copy is (concept ``ad_copy_id`` == copy ``original_id``, compared
as text, like ``creative_agent.concept_guard``), **and** the run's validated
moment allows it: ``trend_situation.timely_ok is True`` with a non-empty
``valid_until``.

It is **expired** when it is timely and ``valid_until`` parses as an ISO datetime
with an offset that is ``<= now``. ``now`` is always the caller's wall clock,
never state ``current_datetime`` (a resumed session keeps its old run date). An
unparseable or offset-less ``valid_until`` never expires.

The rule is pinned by the golden table ``tests/fixtures/timely_expiry.json``,
which the frontend's ``lib/timely.ts`` tests read too.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from runserver.ratings import _as_obj, _id_text, _items


def _situation(state: Mapping[str, Any]) -> Mapping[str, Any]:
    situation = _as_obj(state.get("trend_situation"))
    return situation if isinstance(situation, Mapping) else {}


def valid_until(state: Mapping[str, Any]) -> str:
    """The moment's ``valid_until`` as stored ("" when missing or not a string)."""
    value = _situation(state).get("valid_until")
    return value.strip() if isinstance(value, str) else ""


def _parse_aware(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def is_timely(state: Mapping[str, Any], concept_name: str) -> bool:
    """The concept (or its paired ad copy) references a validated current moment."""
    situation = _situation(state)
    if situation.get("timely_ok") is not True or not valid_until(state):
        return False
    concept = next(
        (
            vc
            for vc in _items(state.get("final_visual_concepts"), "visual_concepts")
            if vc.get("concept_name") == concept_name
        ),
        None,
    )
    if concept is None:
        return False
    if concept.get("uses_trend_state") is True:
        return True
    copy_id = _id_text(concept.get("ad_copy_id"))
    if copy_id is None:
        return False
    return any(
        _id_text(copy.get("original_id")) == copy_id
        and copy.get("uses_trend_state") is True
        for copy in _items(state.get("ad_copy_critique"), "ad_copies")
    )


def timely_expired(
    state: Mapping[str, Any], concept_name: str, now: dt.datetime
) -> bool:
    """A timely concept whose moment ended at or before ``now`` (aware wall clock)."""
    if not is_timely(state, concept_name):
        return False
    until = _parse_aware(valid_until(state))
    return until is not None and until <= now
