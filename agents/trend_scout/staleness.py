"""Staleness of Google's public trends table (``gtrends_staleness``).

trend_scout always reads the newest ``refresh_date`` in
``bigquery-public-data.google_trends.top_terms``; Google sometimes stops
updating it for days. These pure helpers compare that date with the run's
``current_date`` (TREND_TIMEZONE) so the run can say its trends may be old.
"""

import datetime
from typing import Any


def gtrends_staleness(
    refresh_date: str, current_date: str, threshold_days: int
) -> dict[str, Any]:
    """``{refresh_date, age_days, stale, threshold_days}`` for two ISO dates.

    ``age_days`` = days from the table's refresh date to the run's date (never
    negative: a run date behind the table is age 0). Stale when the age is
    greater than ``threshold_days``.
    """
    refresh = datetime.date.fromisoformat(refresh_date)
    today = datetime.date.fromisoformat(current_date)
    age_days = max(0, (today - refresh).days)
    return {
        "refresh_date": refresh.isoformat(),
        "age_days": age_days,
        "stale": age_days > threshold_days,
        "threshold_days": threshold_days,
    }


def staleness_note(staleness: dict[str, Any]) -> str:
    """One brace-free sentence for a stale table (the handoff's note), else ""."""
    if not staleness.get("stale"):
        return ""
    refresh = datetime.date.fromisoformat(staleness["refresh_date"])
    age = int(staleness["age_days"])
    unit = "day" if age == 1 else "days"
    return (
        f"Google's trend data was last updated {refresh:%b} {refresh.day} "
        f"({age} {unit} ago); trends may be out of date."
    )
