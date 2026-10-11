"""agent_common/clock.py: the per-run date state helper and its config/deploy wiring."""

from datetime import UTC, datetime
from pathlib import Path

from agent_common.clock import DEFAULT_TIMEZONE, date_state, now_in_tz


def test_date_state_uses_timezone():
    now = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)  # 23:00 on the 11th in New York
    s = date_state("America/New_York", now=now)
    assert s["current_date"] == "2026-10-11"
    assert s["current_timezone"] == "America/New_York"
    assert s["current_weekday"] == "Sunday"
    assert s["current_datetime"] == "2026-10-11T23:00:00-04:00"


def test_date_state_other_timezone():
    now = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
    s = date_state("Asia/Tokyo", now=now)
    assert s["current_date"] == "2026-10-12"
    assert s["current_weekday"] == "Monday"
    assert s["current_datetime"].endswith("+09:00")


def test_invalid_timezone_falls_back():
    s = date_state("Mars/Base", now=datetime(2026, 1, 1, tzinfo=UTC))
    assert s["current_timezone"] == "America/New_York"
    assert s["current_date"] == "2025-12-31"


def test_blank_timezone_falls_back():
    s = date_state("", now=datetime(2026, 1, 1, tzinfo=UTC))
    assert s["current_timezone"] == DEFAULT_TIMEZONE


def test_naive_now_is_treated_as_utc():
    s = date_state("America/New_York", now=datetime(2026, 10, 12, 3, 0))
    assert s["current_date"] == "2026-10-11"


def test_date_state_keys():
    assert set(date_state("UTC")) == {
        "current_date",
        "current_datetime",
        "current_timezone",
        "current_weekday",
    }


def test_now_in_tz_is_aware():
    assert now_in_tz("Europe/London").tzinfo is not None
    assert now_in_tz("nope/nope").tzinfo is not None


def test_config_reads_trend_timezone(monkeypatch):
    from agent_common.config import BaseAgentConfiguration

    monkeypatch.delenv("TREND_TIMEZONE", raising=False)
    assert BaseAgentConfiguration().trend_timezone == "America/New_York"
    monkeypatch.setenv("TREND_TIMEZONE", "Europe/Paris")
    assert BaseAgentConfiguration().trend_timezone == "Europe/Paris"
    monkeypatch.setenv("TREND_TIMEZONE", "  ")
    assert BaseAgentConfiguration().trend_timezone == "America/New_York"


def test_trend_timezone_shipped_to_agent_engine_and_documented():
    import deployment.deploy_agent as da

    assert da.ENV_VAR_DICT["TREND_TIMEZONE"]
    env_example = (Path(__file__).parent.parent / ".env.example").read_text()
    assert "TREND_TIMEZONE" in env_example
