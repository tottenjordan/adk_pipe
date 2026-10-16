"""Vertex AI Custom Prediction Routine (CPR) serving for the bandit endpoint.

``bandit_serving.predictor.BanditPredictor`` holds one in-memory linear-TS
posterior per endpoint (contracts §2). Like ``bandit``'s JAX modules it is never
imported by ``runserver/`` or the agents; it only runs inside the CPR image
built by ``deployment/bandit/build_image.py`` (and in tests).
"""
