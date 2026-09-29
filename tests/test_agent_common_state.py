"""Tests for agent_common.state (shared memorize tool + initial-state seeding)."""

import re
from types import SimpleNamespace

from agent_common.state import memorize, seed_initial_state


class TestMemorize:
    def test_stores_value_and_reports_status(self):
        ctx = SimpleNamespace(state={})
        result = memorize("brand", "PRS", ctx)  # ty: ignore[invalid-argument-type]
        assert ctx.state == {"brand": "PRS"}
        assert result == {"status": 'Stored "brand": "PRS"'}

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
