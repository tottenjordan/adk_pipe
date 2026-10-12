"""The notebook-parity script: figures from committed fixture data, plus a tiny
generation smoke (one run, two policies).

Fixture: ``tests/fixtures/bandit_parity/`` (regenerate with its
``regenerate.py`` when a plot reads a new field or a figure is added).
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DATA = ROOT / "tests/fixtures/bandit_parity"
FIGURES = [
    "01_cum_avg_reward.png",
    "02_ucb_small_multiplier.png",
    "03_segment_users.png",
    "04_expected_total_reward.png",
    "05_arm_impressions_ctr.png",
    "06_cum_regret.png",
    "07_pct_optimal.png",
    "08_drift_recovery.png",
    "09_arm_injection.png",
]


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "notebook_parity", ROOT / "experiments/bandit/notebook_parity.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fast_compile():
    """XLA compilation dominates a tiny-T run; skipping most optimisations
    roughly halves it. Restored afterwards."""
    import jax

    old = jax.config.read("jax_disable_most_optimizations")
    jax.config.update("jax_disable_most_optimizations", True)
    yield
    jax.config.update("jax_disable_most_optimizations", old)


def test_parity_figures_from_fixture_data(tmp_path):
    # plot_all only reads the data dir, so the fixture is used in place.
    fig_dir = tmp_path / "figs"
    rc = _load_module().main(
        ["--data-dir", str(FIXTURE_DATA), "--fig-dir", str(fig_dir)]
    )
    assert rc == 0
    for name in FIGURES:
        path = fig_dir / name
        assert path.exists() and path.stat().st_size > 1000, name


@pytest.mark.slow
def test_parity_generation_smoke(tmp_path, monkeypatch, fast_compile):
    mod = _load_module()
    full_runs = mod.runs

    def one_run(scale: float) -> dict:
        entry = full_runs(scale)["clear_winner"]
        return {"clear_winner": {**entry, "policies": ["linear_ts", "oracle"]}}

    monkeypatch.setattr(mod, "runs", one_run)
    # main() always plots every figure after generating, which needs all runs;
    # call the generation step alone.
    mod.generate(tmp_path, episodes=1, scale=0.001, seed=0)
    assert [p.name for p in tmp_path.glob("*.json")] == ["clear_winner.json"]
    doc = json.loads((tmp_path / "clear_winner.json").read_text())
    assert doc["aggregate"]["policies"] == ["linear_ts", "oracle"]
    assert {r["policy"] for r in doc["rows"]} == {"linear_ts", "oracle"}
