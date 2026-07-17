"""Offline-first harness for the creative_agent quota-bucket-spread DoE.

Executes the pre-registered design in
``docs/experiments/2026-07-17-quota-bucket-spread-doe.md``: fires N *concurrent*
creative_agent runs per (arm, load) cell against isolated Cloud Run revisions
that differ only by ``CAMPAIGN_RESEARCH_PLACEMENT``, then quantifies the
research-phase latency-inflation slope vs concurrency (primary), corroborating
429 counts, and per-run ``creative_eval`` quality (free, harvested from state).

Reuses the proven network + parse helpers from
:mod:`experiments.creative_latency` (``run_trial``, ``parse_run``, ``fixtures``);
the ONE capability it adds is concurrency — the latency harness is strictly
serial by design because there contention was noise, whereas here it is signal.

Like ``creative_latency``, this package is deliberately NOT imported by any agent
so it is never bundled into an Agent Engine deployment (``deploy_agent.py``'s
``AGENT_EXTRA_PACKAGES`` is derived from the agent import graph). Keep it out.
"""
