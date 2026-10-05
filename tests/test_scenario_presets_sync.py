"""The frontend's generated scenario presets match bandit/ (no JAX needed).

``frontend/src/lib/scenario-presets.generated.json`` feeds the Deploy panel's
Advanced sliders and its live preview; regenerate it with
``uv run python scripts/gen_scenario_presets.py`` after changing a scenario YAML,
``BASE_MARGINALS``, ``OVERRIDE_BOUNDS`` or the feature spec.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from bandit.config import OVERRIDE_BOUNDS, SCENARIOS, load_scenario

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "gen_scenario_presets", ROOT / "scripts" / "gen_scenario_presets.py"
)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_committed_json_is_up_to_date():
    committed = gen.OUTPUT.read_text()
    assert committed == gen.render(), (
        "frontend/src/lib/scenario-presets.generated.json is stale: "
        "run `uv run python scripts/gen_scenario_presets.py`"
    )


def test_check_mode_passes_on_committed_json():
    assert gen.main(["--check"]) == 0


def test_json_carries_every_scenario_and_bound():
    data = json.loads(gen.OUTPUT.read_text())
    assert set(data["scenarios"]) == set(SCENARIOS)
    assert data["overrideBounds"] == {k: list(v) for k, v in OVERRIDE_BOUNDS.items()}
    for name in SCENARIOS:
        sc = load_scenario(name)
        js = data["scenarios"][name]
        assert [s["name"] for s in js["segments"]] == [s.name for s in sc.segments]
        assert abs(sum(s["weight"] for s in js["segments"]) - 1.0) < 1e-12
        assert js["drift"]["atFrac"] == sc.drift.at_frac
    assert len(data["featureNames"]) == 19
