"""Shared Vertex location for model calls (no heavy dependencies).

gemini-3.x models are served ONLY from the `global` Vertex location. Locally we
set ``GOOGLE_CLOUD_LOCATION=global`` and everything works, but inside a deployed
Agent Engine that variable is **reserved**: Agent Engine rejects it in
``env_vars`` and auto-injects it as the engine's own (regional) location, e.g.
``us-central1``. A gemini-3.x call against a regional endpoint returns
``404 NOT_FOUND``, which is what killed every deployed pipeline run.

``MODEL_LOCATION`` is therefore driven by the NON-reserved ``MODEL_LOCATION`` env
var (default ``global``) so it survives Agent Engine deploys — unset there, it
falls back to ``global`` — while still being overridable. It is intentionally
decoupled from ``GCP_REGION`` / regional resources (BigQuery, GCS, Agent Engine),
which stay in ``us-central1``.

This module deliberately has no ADK/genai imports so it can be used from the
standalone (ADK-free) ``creative_eval`` pipeline as well as the agents.
"""

import os

MODEL_LOCATION = os.getenv("MODEL_LOCATION", "global")
