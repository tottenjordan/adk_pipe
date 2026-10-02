"""Policy registry: the ``linear_ts`` adapter + ``make_policy(spec)``.

Policy specs are ``name`` or ``name:key=value[,key=value]`` (e.g.
``ucb1:c=0.01``, ``linear_ts:discount=0.98``); the spec string becomes the
policy label in simulator output, so variants can be compared side by side.
Names follow the contract: ``linear_ts``, ``ucb1``, ``epsilon_greedy``,
``beta_bernoulli_ts``, ``uniform``, ``oracle``.
"""

from __future__ import annotations

import dataclasses

import jax
import jax.numpy as jnp

from bandit import baselines
from bandit import linear_ts as lts
from bandit.baselines import Policy
from bandit.config import LinTSParams, validate_lints_params

POLICY_NAMES: tuple[str, ...] = (
    "linear_ts",
    "ucb1",
    "epsilon_greedy",
    "beta_bernoulli_ts",
    "uniform",
    "oracle",
)

#: Defaults chosen for click-scale rewards (CTR ~ 4%): the notebooks' UCB
#: ``epsilon`` (2 for CTR ~ 0.2, 50 for watch-time scale) shrinks accordingly.
DEFAULT_UCB_C = 0.25
DEFAULT_EPSILON = 0.1


def linear_ts(params: LinTSParams, log_propensity: bool = True) -> Policy:
    """Adapter exposing ``bandit.linear_ts`` through the ``Policy`` interface.

    With ``log_propensity`` each round's propensity is the Monte Carlo
    ``propensities(...)[chosen]`` (``params.propensity_samples`` draws, floored);
    otherwise NaN (cheaper).
    """

    def init(num_arms, dim):
        return lts.init_state(num_arms, dim, params.prior_var)

    def select(key, state, X, eligible, mean_reward):
        del mean_reward
        k_sel, k_prop = jax.random.split(key)
        arms, _ = lts.select(k_sel, state, X, params, eligible)
        if not log_propensity:
            return arms, jnp.full(arms.shape, jnp.nan)
        probs = lts.propensities_batch(k_prop, state, X, params, eligible)
        return arms, jnp.take_along_axis(probs, arms[:, None], axis=1)[:, 0]

    def update(state, arms, X, rewards):
        return lts.update(state, arms, X, rewards, params)

    return Policy("linear_ts", init, select, update)


#: Short aliases accepted by the CLI (the plan's ``lints,egreedy,bbts``).
ALIASES = {
    "lints": "linear_ts",
    "egreedy": "epsilon_greedy",
    "bbts": "beta_bernoulli_ts",
}


def canonical_spec(spec: str) -> str:
    """Replace a leading alias (``lints:discount=0.9`` -> ``linear_ts:discount=0.9``)."""
    name, sep, rest = spec.strip().partition(":")
    return ALIASES.get(name, name) + sep + rest


def _parse_spec(spec: str) -> tuple[str, dict[str, float]]:
    name, _, rest = spec.partition(":")
    kwargs: dict[str, float] = {}
    for part in filter(None, rest.split(",")):
        key, sep, value = part.partition("=")
        if not sep:
            raise ValueError(f"bad policy option {part!r} in {spec!r}")
        try:
            kwargs[key.strip()] = float(value)
        except ValueError as exc:
            raise ValueError(f"policy option {part!r} must be numeric") from exc
    return name.strip(), kwargs


def make_policy(
    spec: str,
    *,
    lints_params: LinTSParams,
    reward_mode: str = "click",
    log_propensity: bool = True,
) -> Policy:
    """Build a policy from a spec string (see module docstring)."""
    name, kw = _parse_spec(spec)

    def take(allowed: set[str]) -> dict[str, float]:
        bad = set(kw) - allowed
        if bad:
            raise ValueError(f"policy {name!r} does not accept {sorted(bad)}")
        return kw

    if name == "linear_ts":
        fields = {f.name for f in dataclasses.fields(LinTSParams)}
        opts = take(fields)
        if "propensity_samples" in opts:
            opts["propensity_samples"] = int(opts["propensity_samples"])
        params = validate_lints_params(dataclasses.replace(lints_params, **opts))
        pol = linear_ts(params, log_propensity=log_propensity)
    elif name == "ucb1":
        pol = baselines.ucb1(c=take({"c"}).get("c", DEFAULT_UCB_C))
    elif name == "epsilon_greedy":
        pol = baselines.epsilon_greedy(
            epsilon=take({"epsilon"}).get("epsilon", DEFAULT_EPSILON)
        )
    elif name == "beta_bernoulli_ts":
        take(set())
        pol = baselines.beta_bernoulli_ts(engaged=reward_mode == "engaged")
    elif name == "uniform":
        take(set())
        pol = baselines.uniform()
    elif name == "oracle":
        take(set())
        pol = baselines.oracle()
    else:
        raise ValueError(f"unknown policy {name!r}; one of {POLICY_NAMES}")
    return dataclasses.replace(pol, name=spec)
