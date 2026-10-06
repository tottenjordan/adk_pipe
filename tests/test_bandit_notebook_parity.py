"""Smoke test: the notebook-parity script produces every figure (tiny T)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
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


@pytest.fixture
def fast_compile():
    """XLA compilation dominates a tiny-T run (~25 distinct episode programs);
    skipping most optimisations roughly halves it. Restored afterwards."""
    import jax

    old = jax.config.read("jax_disable_most_optimizations")
    jax.config.update("jax_disable_most_optimizations", True)
    yield
    jax.config.update("jax_disable_most_optimizations", old)


@pytest.mark.slow
def test_parity_figures_produced(tmp_path, fast_compile):
    spec = importlib.util.spec_from_file_location(
        "notebook_parity", ROOT / "experiments/bandit/notebook_parity.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fig_dir = tmp_path / "figs"
    rc = mod.main(
        [
            "--generate",
            "--data-dir",
            str(tmp_path / "data"),
            "--fig-dir",
            str(fig_dir),
            "--episodes",
            "2",
            "--horizon-scale",
            "0.001",
        ]
    )
    assert rc == 0
    for name in FIGURES:
        path = fig_dir / name
        assert path.exists() and path.stat().st_size > 1000, name
