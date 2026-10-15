"""GEMINI_TEMPERATURE_PROFILE: tuned per-agent temperatures or the Gemini 3 default.

The A/B seam (docs/plans/2026-10-15-gemini3-temperature-ab.md): ``tuned`` (the
default) must keep every agent's temperature exactly as it was before the seam
(golden below, recorded from origin/main), ``default`` must leave every agent
at the Gemini 3 default (1.0 or unset).
"""

import json
import logging
import os
import subprocess
import sys
import textwrap

import pytest

from agent_common import sampling

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# One fresh interpreter per app; each lists every LlmAgent alive after the
# imports (an app that imports another app's pipelines lists those too).
APPS = {
    "creative_agent": (
        "creative_agent.agent",
        "creative_agent.sub_agents.trend_situation.agent",
    ),
    "interactive_creative": ("interactive_creative.agent",),
    "trend_scout": ("trend_scout.agent", "trend_scout.digest"),
}

_CREATIVE = [
    ["ad_copy_critic", 0.7],
    ["ad_copy_drafter", 1.5],
    ["ad_copy_reviser", 0.7],
    ["art_director", 0.9],
    ["brief_reviser", 0.7],
    ["brief_writer", 0.7],
    ["campaign_web_planner", None],
    ["campaign_web_searcher", None],
    ["campaign_web_synthesizer", None],
    ["combined_report_composer", None],
    ["combined_web_evaluator", None],
    ["enhanced_combined_searcher", None],
    ["gs_web_planner", None],
    ["gs_web_searcher", None],
    ["gs_web_synthesizer", None],
    ["merge_planners", None],
    ["refined_web_synthesizer", None],
    ["root_agent", 1.0],
    ["trend_situation_searcher", None],
    ["trend_situation_synthesizer", 0.2],
    ["visual_concept_critic", 0.7],
    ["visual_concept_drafter", 1.5],
    ["visual_concept_finalizer", 0.8],
    ["visual_concept_fixer", 0.7],
]

# Recorded from origin/main (cc5686b) before the seam: the unique
# [name, temperature] pairs, sorted (workflow graphs hold copies of some agents,
# and interactive_creative's root shares creative_agent's name and value).
# creative_eval_agent (temperature unset) was dropped from the list when the
# unused agent was deleted.
GOLDEN_TUNED = {
    "creative_agent": _CREATIVE,
    "interactive_creative": [*_CREATIVE, ["visual_concept_reviser", 0.4]],
    "trend_scout": [
        ["classify_trends_agent", 0.0],
        ["gather_trends_agent", 1.0],
        ["pick_trends_agent", 0.4],
        ["trend_describe_searcher", 0.7],
        ["trend_describe_synthesizer", 0.2],
        ["trend_scout", 0.01],
        ["understand_trends_searcher", 1.5],
        ["understand_trends_synthesizer", 1.5],
    ],
}

_PROBE = textwrap.dedent(
    """
    import gc, importlib, json, sys
    for name in sys.argv[1:]:
        importlib.import_module(name)
    from google.adk.agents import LlmAgent
    gc.collect()
    rows = set()
    for obj in gc.get_objects():
        if isinstance(obj, LlmAgent):
            cfg = obj.generate_content_config
            rows.add((obj.name, cfg.temperature if cfg else None))
    print(json.dumps(sorted(rows, key=lambda r: (r[0], str(r[1])))))
    """
)


def _agent_temperatures(app: str, profile: str | None) -> list[list]:
    env = {**os.environ, "GOOGLE_CLOUD_PROJECT": "test-project"}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (ROOT, env.get("PYTHONPATH")) if p)
    env.pop("GEMINI_TEMPERATURE_PROFILE", None)
    if profile is not None:
        env["GEMINI_TEMPERATURE_PROFILE"] = profile
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, *APPS[app]],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestProfileParsing:
    @pytest.mark.parametrize("raw", [None, "", "   ", "tuned", " Tuned "])
    def test_tuned(self, monkeypatch, raw):
        if raw is None:
            monkeypatch.delenv("GEMINI_TEMPERATURE_PROFILE", raising=False)
        else:
            monkeypatch.setenv("GEMINI_TEMPERATURE_PROFILE", raw)
        assert sampling._profile() == "tuned"

    @pytest.mark.parametrize("raw", ["default", " DEFAULT "])
    def test_default(self, monkeypatch, raw):
        monkeypatch.setenv("GEMINI_TEMPERATURE_PROFILE", raw)
        assert sampling._profile() == "default"

    def test_unknown_falls_back_to_tuned_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("GEMINI_TEMPERATURE_PROFILE", "hot")
        with caplog.at_level(logging.WARNING, logger=sampling.__name__):
            assert sampling._profile() == "tuned"
        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        assert "GEMINI_TEMPERATURE_PROFILE" in caplog.text


class TestTemperature:
    def test_tuned_passes_through(self, monkeypatch):
        monkeypatch.setattr(sampling, "PROFILE", "tuned")
        assert sampling.temperature(0.7) == 0.7
        assert sampling.temperature(0.0) == 0.0

    def test_default_is_gemini3_default(self, monkeypatch):
        monkeypatch.setattr(sampling, "PROFILE", "default")
        assert sampling.temperature(0.01) == 1.0
        assert sampling.temperature(1.5) == sampling.GEMINI3_DEFAULT_TEMPERATURE

    def test_profiles(self):
        assert sampling.PROFILES == ("tuned", "default")


@pytest.mark.slow
@pytest.mark.parametrize("app", sorted(APPS))
class TestAgentTemperatures:
    @pytest.mark.parametrize("profile", [None, "tuned"])
    def test_tuned_matches_pre_seam_golden(self, app, profile):
        assert _agent_temperatures(app, profile) == GOLDEN_TUNED[app]

    def test_default_profile_leaves_every_agent_at_one(self, app):
        rows = _agent_temperatures(app, "default")
        assert rows, "no LlmAgent found"
        off = [row for row in rows if row[1] not in (None, 1.0)]
        assert not off, off
        # Same agents as the tuned golden: only the temperatures differ.
        assert [r[0] for r in rows] == [r[0] for r in GOLDEN_TUNED[app]]
