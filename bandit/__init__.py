"""Offline contextual-bandit core (JAX linear Thompson sampling + simulator).

Never imported by ``runserver/`` or the agent packages: JAX lives only in the uv
dev group (and the PR 2/3 serving/traffic images). See
``docs/bandit/contracts.md`` §1 for the binding API.
"""
