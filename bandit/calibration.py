"""Calibration rules shared by the simulator and the endpoint (contracts §2 / §7 / §10).

The policy's reward-noise variance per scenario and reward mode, the forgetting
discount (incl. the per-traffic-run ``default_shift_discount``), the engaged
reward scale and the endpoint's reward / reset-discount bounds, plus the one
shared ``logit`` / ``sigmoid`` pair. ``runserver`` duplicates several of these
formulas (it never imports ``bandit/``; parity-tested).
"""

from __future__ import annotations

import math

import numpy as np

from bandit.scenario import CTR_MODES, load_scenario

#: Upper bound on an engaged-mode reward (unscaled dwell seconds) the endpoint
#: accepts (contracts §2). The simulator's largest dwell mean is about 120 s, so
#: a real dwell never gets near it; it stops a unit mix-up (milliseconds) or a
#: bad caller from overflowing the float32 posterior.
MAX_DWELL_S: float = 3600.0


def logit(p: np.ndarray | float) -> np.ndarray:
    """log(p / (1 - p)) in float64 (arrays or scalars)."""
    p = np.asarray(p, np.float64)
    return np.log(p) - np.log1p(-p)


def sigmoid(z: np.ndarray | float) -> np.ndarray:
    """1 / (1 + exp(-z)) in float64 (arrays or scalars)."""
    return 1.0 / (1.0 + np.exp(-np.asarray(z, np.float64)))


def default_noise_var(ctr: float, reward_mode: str) -> float:
    """Reward variance the linear-Gaussian model should assume at mean CTR ``ctr``.

    click: Bernoulli variance p(1-p). engaged (rewards scaled by the base dwell,
    so dwell ~ Exponential(≈1)): E[r²] - E[r]² = 2p - p². ``LinTSParams``'s 0.25
    default is ~6x the Bernoulli variance at a 4% CTR and over-explores.
    """
    if reward_mode == "engaged":
        return round(2 * ctr - ctr * ctr, 6)
    return round(ctr * (1 - ctr), 6)


def scenario_noise_var(scenario: str, ctr_mode: str, reward_mode: str) -> float:
    """Calibrated σ² for an experiment: ``default_noise_var`` at the scenario's
    target CTR for ``ctr_mode`` (what ``build_sim_config`` uses). The serving
    container applies it when ``experiment.json`` has no ``policy.noise_var``;
    ``runserver/experiments.py`` duplicates it (parity-tested)."""
    return default_noise_var(load_scenario(scenario).target_ctr[ctr_mode], reward_mode)


#: Effective memory, in rounds, of the endpoint's discounted posterior per scenario
#: and ctr mode (contracts §7). Only ``drift`` forgets: a discount costs the
#: stationary scenarios regret (docs/experiments/bandit-simulation.md, "Discount
#: sweep"). The window is 1/8 of the preset horizon (5k of 40k demo rounds, 50k of
#: 400k realistic ones), the flat optimum of that sweep. ``runserver`` duplicates
#: this table and ``discount_for_memory`` (parity-tested).
DISCOUNT_MEMORY_ROUNDS: dict[str, dict[str, int]] = {
    "drift": {"demo": 5000, "realistic": 50000},
}


def discount_for_memory(memory_rounds: float, batch_size: int = 100) -> float:
    """Per-update discount γ for an effective memory of ``memory_rounds`` rounds.

    ``update`` applies γ once per batch, so evidence from ``b`` batches ago keeps
    weight γᵇ; with γ = exp(-batch_size / N) a round ``t`` rounds old keeps
    exp(-t / N), i.e. an exponential window of N rounds (equivalently
    N ≈ batch_size / (1 - γ) for γ near 1). Rounded to 3 decimals."""
    if not memory_rounds > 0 or not batch_size > 0:
        raise ValueError("memory_rounds and batch_size must be > 0")
    return round(math.exp(-batch_size / memory_rounds), 3)


def default_discount(scenario: str, ctr_mode: str, batch_size: int = 100) -> float:
    """The endpoint's ``policy.discount`` for an experiment: forgetting with the
    ``DISCOUNT_MEMORY_ROUNDS`` window where one is set (``drift``), else 1.0."""
    memory = DISCOUNT_MEMORY_ROUNDS.get(scenario, {}).get(ctr_mode)
    return discount_for_memory(memory, batch_size) if memory else 1.0


def reward_scale(scenario: str, reward_mode: str) -> float:
    """Divisor applied to rewards before a policy update: the scenario's base
    dwell for ``engaged`` (so rewards are ~Exponential(1)-scaled, matching
    ``default_noise_var``), 1 for clicks. Mirrors ``TrueModel.reward_scale``."""
    return load_scenario(scenario).dwell_base_s if reward_mode == "engaged" else 1.0


def default_shift_discount(ctr_mode: str, batch_size: int, horizon: int) -> float:
    """The endpoint's discount for a traffic run with shifts when forgetting is
    on (contracts §10): the drift memory rule (``DISCOUNT_MEMORY_ROUNDS``, 1/8
    of the horizon) applied to this run's ``horizon`` via
    ``discount_for_memory``. ``ctr_mode`` is checked for symmetry with
    ``default_discount``; the run's horizon already carries the mode's scale."""
    if ctr_mode not in CTR_MODES:
        raise ValueError(f"ctr_mode must be one of {CTR_MODES}")
    return discount_for_memory(horizon / 8, batch_size)


#: Bounds on a ``reset`` instance's optional ``discount`` (contracts §2 / §10): the
#: per-run forgetting γ the traffic job sends when ``forget`` is on. Short runs
#: (``default_shift_discount`` below 0.95, i.e. horizon < ~15.6k at batch 100)
#: are clamped up to the floor by the job. ``bandit_serving`` and the fake
#: endpoint both validate against this.
RESET_DISCOUNT_BOUNDS: tuple[float, float] = (0.95, 1.0)
