"""Offline contextual-bandit core (JAX linear Thompson sampling + simulator).

Never imported by ``runserver/`` or the agent packages: JAX lives only in the uv
dev group (and the PR 2/3 serving/traffic images). See
``docs/bandit/contracts.md`` §1 for the binding API.

Importing ``bandit`` itself pulls in numpy + PyYAML only (config, features and
the jax-free ``aggregate``); the JAX modules (``linear_ts``, ``baselines``,
``policies``, ``environment``, ``simulate``) are imported explicitly.
"""

from bandit.aggregate import aggregate_episode_metrics
from bandit.config import (
    ArmSpec,
    ExperimentConfig,
    LinTSParams,
    ScenarioConfig,
    build_sim_config,
    default_arms,
    experiment_config_to_dict,
    load_experiment_config,
    load_scenario,
)
from bandit.features import (
    FEATURE_SPEC_VERSION,
    encode_batch,
    encode_context,
    feature_names,
)

__all__ = [
    "FEATURE_SPEC_VERSION",
    "ArmSpec",
    "ExperimentConfig",
    "LinTSParams",
    "ScenarioConfig",
    "aggregate_episode_metrics",
    "build_sim_config",
    "default_arms",
    "encode_batch",
    "encode_context",
    "experiment_config_to_dict",
    "feature_names",
    "load_experiment_config",
    "load_scenario",
]
