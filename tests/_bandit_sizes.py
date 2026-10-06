"""Standard sizes for the bandit tests that run simulator / traffic episodes.

``bandit.simulate.run_episodes`` compiles one XLA program per (policy, batch
size, number of batches, reward mode, arms, episode-chunk shape), and the
predictor's kernels likewise per shape. Tests that pick their own horizon each
pay a fresh ~1-3 s compile per policy; tests on these shared sizes hit the
in-process ``simulate._compiled`` cache or the persistent JAX compilation cache
(``tests/conftest.py``) instead, even across files and xdist workers.

Use ``HORIZON_S`` for plumbing / exact-parity checks, ``HORIZON_M`` when a check
needs a longer episode, and always ``BATCH``. A test that genuinely needs a
different size (a statistical assertion that needs more rounds, a horizon that
must not be a multiple of the batch) keeps it, with a comment saying why.
"""

HORIZON_S = 1_000
HORIZON_M = 4_000
BATCH = 100
