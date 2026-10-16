"""Per-run date state: what "today" is for an agent run.

No agent knew today's date, so "yesterday", "tonight" or "this weekend" in a
search result had no anchor. ``date_state`` is computed once per run when the
session state is seeded (``agent_common.state.seed_initial_state``) — never at
import, because Agent Engine pickles the agents at deploy time and an
import-time date would freeze the deploy day into every run.

The timezone comes from ``TREND_TIMEZONE`` (``BaseAgentConfiguration``,
default ``America/New_York``); an unknown name falls back to the default with a
warning. ADK/genai-free.
"""

import logging
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "America/New_York"

# The shared prompt line every date-aware agent reads (trend research, brief,
# copy, art direction). Optional `{x?}` tokens so a session seeded before the
# date keys existed still renders; the values come from `date_state` at run time.
DATE_CONTEXT_LINE = (
    "Today is {current_date?} ({current_weekday?}, {current_timezone?}). "
    'Interpret "yesterday", "tonight", "tomorrow" and "this weekend" relative to '
    "this date, and never assume an event's timing or result without a dated source."
)

logger = logging.getLogger(__name__)


def _zone(tz_name: str | None) -> tuple[str, tzinfo]:
    """Resolve ``tz_name`` to ``(name, tzinfo)``; unknown/blank → the default zone.

    UTC is the last resort if even the default zone is missing (no tz database).
    """
    name = (tz_name or "").strip()
    if name:
        try:
            return name, ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "Unknown timezone %r; falling back to %s", name, DEFAULT_TIMEZONE
            )
    try:
        return DEFAULT_TIMEZONE, ZoneInfo(DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:  # pragma: no cover - tzdata is a dependency
        logger.warning("No tz database; using UTC")
        return "UTC", UTC


def now_in_tz(tz_name: str | None) -> datetime:
    """The current time in ``tz_name`` (aware; unknown name → the default zone)."""
    return datetime.now(_zone(tz_name)[1])


def date_state(tz_name: str | None, *, now: datetime | None = None) -> dict[str, str]:
    """The run's date keys for session state.

    Args:
        tz_name: IANA timezone name (unknown/blank → ``America/New_York``).
        now: the instant to describe (tests); naive values are taken as UTC.
            Defaults to the current time.

    Returns:
        ``{current_date: "YYYY-MM-DD", current_datetime: ISO 8601 with offset
        (seconds precision), current_timezone, current_weekday}``.
    """
    name, zone = _zone(tz_name)
    if now is None:
        local = datetime.now(zone)
    else:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        local = now.astimezone(zone)
    return {
        "current_date": local.date().isoformat(),
        "current_datetime": local.isoformat(timespec="seconds"),
        "current_timezone": name,
        "current_weekday": local.strftime("%A"),
    }
