"""Synthetic traffic generator for bandit experiments (Cloud Run Job).

Drives a deployed bandit endpoint (contracts §2) with synthetic users drawn from
``bandit.environment`` (common random numbers), replays the baselines locally on
the same users, and logs ``bandit_events`` / ``bandit_episode_metrics`` rows plus
``bandit_experiments.progress`` (contracts §3). Entry point:
``python -m bandit_traffic.main``. Uses JAX via ``bandit``; never imported by the
api or the agents.
"""
