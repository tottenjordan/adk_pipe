"""Tests for agent_common.state (shared memorize tool + initial-state seeding)."""

import re
from datetime import UTC, datetime

from agent_common.state import memorize, seed_initial_state


class TestMemorize:
    def test_tool_name_stays_memorize(self):
        # ADK derives the FunctionTool name from __name__; prompts call `memorize`.
        assert memorize.__name__ == "memorize"

    def test_agent_packages_reexport_the_shared_tool(self):
        from creative_agent import tools as creative_tools
        from trend_scout import tools as scout_tools

        assert creative_tools.memorize is memorize
        assert scout_tools.memorize is memorize


class TestSeedInitialState:
    def _seed(self, target, source=None, **kwargs):
        params = {
            "state_init_key": "_init",
            "gcs_bucket": "gs://bucket",
            "agent_output_dir": "out",
        } | kwargs
        return seed_initial_state(source or {}, target, **params)

    def test_seeds_empty_target(self):
        target = {}
        assert self._seed(target, {"brand": "PRS"}) is True
        assert target["_init"] is True
        assert target["gcs_bucket"] == "gs://bucket"
        assert target["agent_output_dir"] == "out"
        assert target["brand"] == "PRS"
        assert re.fullmatch(
            r"\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_[0-9a-f]{4}", target["gcs_folder"]
        )

    def test_noop_when_already_seeded(self):
        target = {"_init": True, "gcs_folder": "keep"}
        assert self._seed(target, {"brand": "PRS"}) is False
        assert target == {"_init": True, "gcs_folder": "keep"}

    def test_extra_keys_applied_and_source_wins(self):
        target = {}
        self._seed(target, {"shared": "source"}, extra={"x": 1, "shared": "extra"})
        assert target["x"] == 1
        assert target["shared"] == "source"

    def test_source_overwrites_existing_values(self):
        target = {"brand": "old"}
        self._seed(target, {"brand": ""})
        assert target["brand"] == ""


class TestSeedDateState:
    """The run's date keys: computed at seed time (never import), kept on resume."""

    _KEYS = ("current_date", "current_datetime", "current_timezone", "current_weekday")

    def _seed(self, target, **kwargs):
        params = {
            "state_init_key": "_init",
            "gcs_bucket": "gs://bucket",
            "agent_output_dir": "out",
        } | kwargs
        return seed_initial_state({}, target, **params)

    def test_fresh_state_gets_date_keys(self):
        target = {}
        self._seed(
            target,
            timezone="America/New_York",
            now=datetime(2026, 10, 12, 3, 0, tzinfo=UTC),
        )
        assert target["current_date"] == "2026-10-11"
        assert target["current_weekday"] == "Sunday"
        assert target["current_timezone"] == "America/New_York"
        assert target["current_datetime"].startswith("2026-10-11T23:00")

    def test_default_timezone_when_unset(self):
        target = {}
        self._seed(target)
        for key in self._KEYS:
            assert target[key], key
        assert target["current_timezone"] == "America/New_York"

    def test_caller_seeded_date_not_overwritten(self):
        target = {"current_date": "2026-01-02"}
        self._seed(target)
        assert target["current_date"] == "2026-01-02"

    def test_resumed_session_keeps_original_date(self):
        target = {}
        self._seed(target, now=datetime(2026, 10, 1, 12, tzinfo=UTC))
        assert self._seed(target, now=datetime(2026, 10, 9, 12, tzinfo=UTC)) is False
        assert target["current_date"] == "2026-10-01"

    def test_runs_on_different_days_get_different_dates(self):
        first, second = {}, {}
        self._seed(first, now=datetime(2026, 10, 1, 12, tzinfo=UTC))
        self._seed(second, now=datetime(2026, 10, 2, 12, tzinfo=UTC))
        assert first["current_date"] == "2026-10-01"
        assert second["current_date"] == "2026-10-02"

    def test_date_is_computed_per_call_not_at_import(self, monkeypatch):
        """Agent Engine pickles agents at deploy: the date must be read per seed."""
        import agent_common.state as state_mod

        calls = []

        def fake_date_state(tz, *, now=None):
            calls.append(tz)
            return {"current_date": f"day-{len(calls)}"}

        monkeypatch.setattr(state_mod, "date_state", fake_date_state)
        a, b = {}, {}
        self._seed(a, timezone="UTC")
        self._seed(b, timezone="UTC")
        assert (a["current_date"], b["current_date"]) == ("day-1", "day-2")
