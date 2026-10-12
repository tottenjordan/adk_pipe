"""The trend's current moment: a deterministic validator over the model's draft.

The grounded trend_situation branch (sub_agents/trend_situation) drafts a
``TrendSituationDraft`` from dated web sources. ``validate_situation`` turns it
into the run's ``TrendSituation`` by rule, not by the model's judgement: it
abstains (phase ``"unknown"``, ``timely_ok=False``, an ``abstain_reason``)
unless the event is dated, inside the event window around today and backed by
known sources (plus an outcome for a just-happened event). Unknown means no
timely reference, which is the behaviour without this feature, so it fails safe.

Also here (pure, brace-free text):

- ``tone_rule_for`` — the phase-conditioned tone playbook for a timely
  reference (upcoming = anticipation; win = celebration; loss = empathy);
- ``current_moment_md`` — the one-line moment ("Upcoming: …") the brief writer
  reads as ``{trend_situation_md?}`` ("" when the moment is not usable);
- ``resolve_now`` — the run's "now" in its timezone.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from .schemas import TrendSituation, TrendSituationDraft

logger = logging.getLogger(__name__)

DEFAULT_TREND_TIMEZONE = "America/New_York"

# Phases a creative could reference (the others are never timely).
TIMELY_PHASES = frozenset({"upcoming", "just_happened"})
_NOT_TIMELY_REASONS = {
    "live": "live moments change too fast to reference",
    "sensitive": "sensitive moment: never referenced",
    "ongoing": "no single dated event",
    "evergreen": "no time-bound event",
}
_PHASE_LABELS = {"just_happened": "just happened"}
_URL_DATE = re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)")
_BRACES = str.maketrans("", "", "{}")


# --- now --- #
def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo((name or "").strip() or DEFAULT_TREND_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown timezone %r; using %s", name, DEFAULT_TREND_TIMEZONE)
        return ZoneInfo(DEFAULT_TREND_TIMEZONE)


def _default_tz_name() -> str:
    """``TREND_TIMEZONE`` (``BaseAgentConfiguration.trend_timezone``)."""
    from .config import config

    return config.trend_timezone or DEFAULT_TREND_TIMEZONE


def resolve_now(state: Mapping[str, Any], *, tz_name: str | None = None) -> datetime:
    """The run's "now", aware, in the run's timezone.

    State ``current_datetime`` (ISO, seeded once per run by
    ``agent_common.state.seed_initial_state`` via ``agent_common.clock``) wins,
    so a resumed run keeps its original date; else the wall clock. The timezone is state
    ``current_timezone``, else ``tz_name``, else ``TREND_TIMEZONE`` (default
    America/New_York); an invalid name falls back to the default.
    """
    zone = _zone(
        str(state.get("current_timezone") or "") or tz_name or _default_tz_name()
    )
    raw = state.get("current_datetime")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip())
        except ValueError:
            parsed = None
        if parsed is not None:
            return (
                parsed.astimezone(zone)
                if parsed.tzinfo
                else parsed.replace(tzinfo=zone)
            )
    return datetime.now(zone)


# --- tone playbook (Idea 4) --- #
def tone_rule_for(phase: str, outcome: str) -> str:
    """The tone for a timely reference to this moment ("" when never timely)."""
    if phase == "upcoming":
        return (
            "anticipation, fan rituals and countdown energy (game-day ready); "
            "never predict the score or the result"
        )
    if phase != "just_happened":
        return ""
    if outcome == "win":
        return (
            "celebration and pride, bragging rights; never gloat at rivals or "
            "mock the losing side"
        )
    if outcome == "loss":
        return (
            "empathy and loyalty, comfort, still with you; no urgency or discount "
            "pushes, no jokes at the team's expense, and never the words lose or "
            "lost in a headline"
        )
    return "acknowledge the moment factually and respectfully; no exaggeration"


# --- parsing --- #
def _parse_draft(draft: Any) -> TrendSituationDraft | None:
    if isinstance(draft, TrendSituationDraft):
        return draft
    if isinstance(draft, str):
        if not draft.strip():
            return None
        try:
            draft = json.loads(draft)
        except ValueError:
            return None
    if not isinstance(draft, Mapping):
        return None
    try:
        return TrendSituationDraft.model_validate(dict(draft))
    except ValidationError:
        return None


def _parse_when(raw: str, zone: Any) -> tuple[date, datetime | None] | None:
    """(event date in the run's timezone, exact datetime or None for a date)."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if len(text) <= 10:  # a bare date
        return parsed.date(), None
    moment = parsed.astimezone(zone) if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment.date(), moment


def _end_of_day(day: date, zone: Any) -> datetime:
    return datetime.combine(day, time(23, 59, 59), tzinfo=zone)


def _url_date(url: str) -> date | None:
    match = _URL_DATE.search(url or "")
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# --- validator --- #
def _situation(fields: Mapping[str, Any], **extra: Any) -> TrendSituation:
    return TrendSituation.model_validate({**fields, **extra})


def validate_situation(
    draft: Any,
    *,
    sources: Mapping[str, Any] | None,
    now: datetime,
    window_days: int,
    valid_hours_after: int,
    enabled: bool = True,
) -> TrendSituation:
    """The validated current moment for a (possibly missing) draft (pure).

    Abstains to ``phase="unknown"`` (``timely_ok=False`` + ``abstain_reason``)
    when: disabled; no/invalid draft; an upcoming/just-happened event without a
    parseable date inside ``window_days`` of today (upcoming: today or later;
    just happened: today or earlier, with an outcome); a cited id missing from
    ``sources``; no source for an upcoming/just-happened event; or a cited
    source whose URL date predates a just-happened event by more than a day.
    ``live``/``sensitive``/``ongoing``/``evergreen`` and low confidence keep
    their phase but are never timely; a sensitive moment keeps only its known
    source ids (it only ever restricts the work, so it is not downgraded).
    ``valid_until``: upcoming → end of the event day; just happened → the
    event end (or start; a bare date = end of that day) + ``valid_hours_after``;
    an expired moment is not timely.
    """
    zone = now.tzinfo or _zone(None)
    as_of = _iso(now)

    def abstain(reason: str, base: TrendSituationDraft | None = None) -> TrendSituation:
        # Keep the drafted event fields for observability, never its outcome.
        fields = base.model_dump() if base is not None else {}
        fields.update(phase="unknown", outcome="", outcome_summary="")
        return _situation(fields, as_of=as_of, timely_ok=False, abstain_reason=reason)

    if not enabled:
        return abstain("disabled")
    parsed = _parse_draft(draft)
    if parsed is None:
        return abstain("no trend situation was drafted")
    if parsed.phase == "unknown":
        return abstain("the sources did not date the event", parsed)

    known = sources if isinstance(sources, Mapping) else {}
    cited = [s.strip() for s in parsed.source_ids if isinstance(s, str) and s.strip()]
    unknown_ids = [s for s in cited if s not in known]

    if parsed.phase not in TIMELY_PHASES:
        kept = [s for s in cited if s in known]
        if unknown_ids and parsed.phase != "sensitive":
            return abstain(f"cites unknown sources: {', '.join(unknown_ids)}", parsed)
        return _situation(
            {**parsed.model_dump(), "source_ids": kept},
            as_of=as_of,
            timely_ok=False,
            abstain_reason=_NOT_TIMELY_REASONS.get(parsed.phase, "not timely"),
        )

    if unknown_ids:
        return abstain(f"cites unknown sources: {', '.join(unknown_ids)}", parsed)
    if not cited:
        return abstain("no source dates the event", parsed)
    start = _parse_when(parsed.event_start, zone)
    if start is None:
        return abstain("no parseable event date", parsed)
    start_day, start_at = start
    today = now.date()
    offset = (start_day - today).days
    if abs(offset) > window_days:
        return abstain(f"the event is more than {window_days} days from today", parsed)

    if parsed.phase == "upcoming":
        if offset < 0:
            return abstain("an upcoming event dated in the past", parsed)
        valid_until = _end_of_day(start_day, zone)
    else:  # just_happened
        if offset > 0:
            return abstain("a just-happened event dated in the future", parsed)
        if not parsed.outcome:
            return abstain("a just-happened event without an outcome", parsed)
        earliest = start_day - timedelta(days=1)
        for source_id in cited:
            source = known.get(source_id)
            url = source.get("url", "") if isinstance(source, Mapping) else ""
            published = _url_date(str(url))
            if published is not None and published < earliest:
                return abstain(f"{source_id} predates the event", parsed)
        end = _parse_when(parsed.event_end, zone) or start
        end_day, end_at = end
        valid_until = (end_at or _end_of_day(end_day, zone)) + timedelta(
            hours=valid_hours_after
        )

    fields = parsed.model_dump()
    reason = ""
    if parsed.confidence == "low":
        reason = "low confidence"
    elif valid_until <= now:
        reason = "the moment has expired"
    return _situation(
        fields,
        as_of=as_of,
        timely_ok=not reason,
        valid_until=_iso(valid_until),
        tone_rule="" if reason else tone_rule_for(parsed.phase, parsed.outcome),
        abstain_reason=reason,
    )


# --- rendering --- #
def as_situation(value: Any) -> TrendSituation | None:
    """A ``TrendSituation`` from a model or its dict (None when invalid)."""
    if isinstance(value, TrendSituation):
        return value
    if isinstance(value, Mapping):
        try:
            return TrendSituation.model_validate(dict(value))
        except ValidationError:
            return None
    return None


def _clean(text: str) -> str:
    return " ".join(str(text or "").translate(_BRACES).split())


def _short_time(iso: str) -> str:
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return _clean(iso)
    zone = moment.strftime("%Z")
    return moment.strftime("%Y-%m-%d %H:%M") + (f" {zone}" if zone else "")


def current_moment_md(situation: Any) -> str:
    """One brace-free line for ``{trend_situation_md?}`` ("" when not usable).

    A timely moment: "<Phase>: <event>, <date>. <outcome>
    (sources: …; valid until …)". A sensitive moment is rendered too, with an
    instruction never to reference it; every other (unknown, not timely) moment
    renders "" so the brief cannot lean on it.
    """
    data = as_situation(situation)
    if data is None:
        return ""
    if not (data.timely_ok or data.phase == "sensitive"):
        return ""
    label = _PHASE_LABELS.get(data.phase, data.phase)
    head = f"{label.capitalize()}: {_clean(data.event_name) or 'the trend'}"
    if data.event_start:
        head += f", {_clean(data.event_start)}"
    parts = [head + "."]
    if data.outcome_summary:
        parts.append(_clean(data.outcome_summary))
    if data.phase == "sensitive":
        parts.append("Never reference this moment in the creative work.")
    tail = []
    if data.source_ids:
        tail.append("sources: " + ", ".join(_clean(s) for s in data.source_ids))
    if data.timely_ok and data.valid_until:
        tail.append(f"valid until {_short_time(data.valid_until)}")
    line = " ".join(parts)
    return f"{line} ({'; '.join(tail)})" if tail else line


def timely_allowed(state: Any, max_timely: int) -> bool:
    """True when the run's validated moment is timely and the slot is on.

    ``state`` is a Mapping or an ADK ``State`` (anything with ``.get``).

    The guards' switch: the timely cap and the evergreen temporal-term checks
    apply only then, so runs without a timely moment are checked exactly as
    before.
    """
    situation = state.get("trend_situation")
    return (
        max_timely > 0
        and isinstance(situation, Mapping)
        and situation.get("timely_ok") is True
    )


def _relative_day(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    return f"in {days} days" if days > 0 else f"{-days} days ago"


def moment_phrase(situation: Any) -> str:
    """The timely moment in plain words for the timely rules ("" if not timely).

    "Texas vs Oklahoma is tomorrow (Oct 12)" / "Texas vs Oklahoma was yesterday
    (Oct 10): Texas lost to Oklahoma 24-31." — relative to the validation
    time (``as_of``), in its timezone. Brace-free.
    """
    data = as_situation(situation)
    if data is None or not data.timely_ok:
        return ""
    event = _clean(data.event_name) or "the event"
    try:
        as_of = datetime.fromisoformat(data.as_of)
    except ValueError:
        as_of = None
    zone = as_of.tzinfo if as_of is not None and as_of.tzinfo else _zone(None)
    when = _parse_when(data.event_start, zone)
    if when is None or as_of is None:
        return event
    day = when[0]
    relative = _relative_day((day - as_of.astimezone(zone).date()).days)
    dated = f"{relative} ({day:%b} {day.day})"
    if data.phase == "upcoming":
        return f"{event} is {dated}"
    phrase = f"{event} was {dated}"
    summary = _clean(data.outcome_summary)
    return f"{phrase}: {summary}" if summary else phrase


def trend_situation_state_delta(
    state: Mapping[str, Any],
    *,
    enabled: bool,
    window_days: int,
    valid_hours_after: int,
    max_timely: int = 0,
) -> dict[str, Any]:
    """The validator node's state delta for a state snapshot (pure).

    Reads the branch's ``trend_situation_draft`` and ``sources`` and writes
    ``trend_situation`` (the validated dict) + ``trend_situation_md``, plus
    ``timely_rules`` (the timely slot's rules for the creative agents, "" unless
    the moment is timely and ``max_timely`` > 0).
    """
    from .prompts import build_timely_rules

    sources = state.get("sources")
    situation = validate_situation(
        state.get("trend_situation_draft"),
        sources=sources if isinstance(sources, Mapping) else None,
        now=resolve_now(state),
        window_days=window_days,
        valid_hours_after=valid_hours_after,
        enabled=enabled,
    )
    return {
        "trend_situation": situation.model_dump(),
        "trend_situation_md": current_moment_md(situation),
        "timely_rules": build_timely_rules(situation, max_timely),
    }
