"""Stale Google Trends warning: trend_scout flags an old public-table refresh.

``get_daily_gtrends`` reads ``MAX(refresh_date)`` from Google's public table,
which sometimes stops updating for days. When that date is more than
``GTRENDS_STALE_AFTER_DAYS`` (default 2) behind the run's ``current_date``
(TREND_TIMEZONE), the tool records ``gtrends_staleness`` + a brace-free
``gtrends_staleness_note``, logs one WARNING, and the root's handoff mentions it.
"""

import asyncio
import datetime
import logging
from pathlib import Path

import pytest

from tests._fakes import FakeToolContext

ROOT = Path(__file__).parent.parent


class TestStalenessHelper:
    def test_stale_when_older_than_threshold(self):
        from trend_scout.staleness import gtrends_staleness

        value = gtrends_staleness("2026-10-13", "2026-10-19", 2)
        assert value == {
            "refresh_date": "2026-10-13",
            "age_days": 6,
            "stale": True,
            "threshold_days": 2,
        }

    @pytest.mark.parametrize(
        ("refresh", "age", "stale"),
        [
            ("2026-10-19", 0, False),
            ("2026-10-18", 1, False),
            ("2026-10-17", 2, False),  # exactly the threshold: not stale yet
            ("2026-10-16", 3, True),
        ],
    )
    def test_boundary(self, refresh, age, stale):
        from trend_scout.staleness import gtrends_staleness

        value = gtrends_staleness(refresh, "2026-10-19", 2)
        assert (value["age_days"], value["stale"]) == (age, stale)

    def test_future_refresh_date_is_age_zero(self):
        # A run date behind the table (timezone skew) is never stale or negative.
        from trend_scout.staleness import gtrends_staleness

        value = gtrends_staleness("2026-10-20", "2026-10-19", 2)
        assert value["age_days"] == 0
        assert value["stale"] is False

    def test_note_only_when_stale(self):
        from trend_scout.staleness import staleness_note

        stale = {"refresh_date": "2026-10-13", "age_days": 6, "stale": True}
        assert staleness_note(stale) == (
            "Google's trend data was last updated Oct 13 (6 days ago); "
            "trends may be out of date."
        )
        assert staleness_note({**stale, "stale": False}) == ""
        assert "{" not in staleness_note(stale) and "}" not in staleness_note(stale)

    def test_note_singular_day(self):
        from trend_scout.staleness import staleness_note

        note = staleness_note(
            {"refresh_date": "2026-10-18", "age_days": 1, "stale": True}
        )
        assert "(1 day ago)" in note


class TestThresholdKnob:
    def test_parser_defaults_and_clamps(self):
        from trend_scout.config import parse_gtrends_stale_after_days

        assert parse_gtrends_stale_after_days(None) == 2
        assert parse_gtrends_stale_after_days("") == 2
        assert parse_gtrends_stale_after_days("abc") == 2
        assert parse_gtrends_stale_after_days("4") == 4
        assert parse_gtrends_stale_after_days("0") == 1
        assert parse_gtrends_stale_after_days("999") == 30

    def test_config_reads_env_per_instance(self, monkeypatch):
        from trend_scout.config import ResearchConfiguration

        monkeypatch.delenv("GTRENDS_STALE_AFTER_DAYS", raising=False)
        assert ResearchConfiguration().gtrends_stale_after_days == 2
        monkeypatch.setenv("GTRENDS_STALE_AFTER_DAYS", "5")
        assert ResearchConfiguration().gtrends_stale_after_days == 5

    def test_shipped_to_agent_engine_and_documented(self):
        import deployment.deploy_agent as da

        key = "GTRENDS_STALE_AFTER_DAYS"
        assert da.ENV_VAR_DICT[key]
        assert key in (ROOT / ".env.example").read_text()
        assert f"{key}=" in (ROOT / "deployment" / "deploy.env").read_text()
        assert key in (ROOT / "docs" / "reference" / "configuration.md").read_text()


def _patch_bq(monkeypatch, max_date: str):
    import trend_scout.tools as t

    refresh = datetime.datetime.strptime(max_date, "%m/%d/%Y").date()

    class _Job:
        def result(self, timeout=None):
            return [{"term": "a", "refresh_date": refresh, "x": [{"rank": 1}]}]

    class _Client:
        def query(self, sql, job_config=None):
            return _Job()

    monkeypatch.setattr(t, "_get_bigquery_client", lambda: _Client())
    monkeypatch.setattr(t, "_get_gtrends_max_date", lambda: max_date)
    return t


class TestGetDailyGtrendsRecordsStaleness:
    @pytest.fixture(autouse=True)
    def _wall_clock_elsewhere(self, monkeypatch):
        # The wall clock disagrees with the seeded current_date, so a tool that
        # ignored the run's date would compute a different age.
        import trend_scout.tools as t

        monkeypatch.setattr(t, "now_in_tz", lambda tz: datetime.datetime(2030, 1, 1))

    def test_stale_table_records_state_and_warns_once(self, monkeypatch, caplog):
        t = _patch_bq(monkeypatch, "10/13/2026")
        ctx = FakeToolContext({"current_date": "2026-10-19"})
        with caplog.at_level(logging.WARNING):
            asyncio.run(t.get_daily_gtrends(ctx))
            # A retried/repeated gather of the same stale date does not re-warn.
            asyncio.run(t.get_daily_gtrends(ctx))

        assert ctx.state["gtrends_staleness"] == {
            "refresh_date": "2026-10-13",
            "age_days": 6,
            "stale": True,
            "threshold_days": 2,
        }
        assert ctx.state["gtrends_staleness_note"].startswith(
            "Google's trend data was last updated Oct 13 (6 days ago)"
        )
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "stale" in r.getMessage().lower()
        ]
        assert len(warnings) == 1

    def test_fresh_table_records_not_stale_and_no_warning(self, monkeypatch, caplog):
        t = _patch_bq(monkeypatch, "10/18/2026")
        ctx = FakeToolContext({"current_date": "2026-10-19"})
        with caplog.at_level(logging.WARNING):
            asyncio.run(t.get_daily_gtrends(ctx))

        assert ctx.state["gtrends_staleness"]["stale"] is False
        assert ctx.state["gtrends_staleness"]["age_days"] == 1
        assert ctx.state["gtrends_staleness_note"] == ""
        assert not [r for r in caplog.records if "stale" in r.getMessage().lower()]

    def test_threshold_comes_from_config(self, monkeypatch):
        t = _patch_bq(monkeypatch, "10/13/2026")
        monkeypatch.setattr(t.config, "gtrends_stale_after_days", 7)
        ctx = FakeToolContext({"current_date": "2026-10-19"})
        asyncio.run(t.get_daily_gtrends(ctx))
        assert ctx.state["gtrends_staleness"]["stale"] is False
        assert ctx.state["gtrends_staleness"]["threshold_days"] == 7

    def test_no_current_date_falls_back_to_today_in_trend_timezone(self, monkeypatch):
        t = _patch_bq(monkeypatch, "10/13/2026")
        seen = []

        def fake_now(tz):
            seen.append(tz)
            return datetime.datetime(2026, 10, 14, 9, 0)

        monkeypatch.setattr(t, "now_in_tz", fake_now)
        ctx = FakeToolContext()
        asyncio.run(t.get_daily_gtrends(ctx))
        assert seen == [t.config.trend_timezone]
        assert ctx.state["gtrends_staleness"]["age_days"] == 1


def test_root_handoff_mentions_stale_data_only_when_noted():
    from trend_scout.prompts import TREND_SCOUT_INSTR

    assert "{gtrends_staleness_note?}" in TREND_SCOUT_INSTR
    handoff = TREND_SCOUT_INSTR.split("### Phase 4: Handoff", 1)[1]
    assert "{gtrends_staleness_note?}" in handoff
    assert "gtrends_staleness_note is non-empty" in handoff
