"""Create (or reuse) the dedicated Agent Engine that backs persistent ADK sessions.

The Cloud Run backend runs `adk api_server agents --session_service_uri
agentengine://<resource>`, which builds a `VertexAiSessionService`. That service
stores sessions *inside* a Reasoning Engine (Agent Engine). We give it a
**dedicated** engine — `trend-trawler-sessions` — that serves no agent, so the
session store's lifetime is decoupled from any served-agent deploy.

The engine is created with no `agent_engine` payload (the SDK allows a bare
container) in `us-central1`, which pins the session store to the region while the
gemini-3.x models stay pinned to `global` in code. The printed, fully-qualified
resource name is what ships as `SESSION_SERVICE_URI=agentengine://<resource>`.

Idempotent: reuses an existing engine with the same display name if one exists.

Usage:
    uv run python deployment/create_session_engine.py
"""

from __future__ import annotations

import vertexai
from vertexai import agent_engines

PROJECT = "hybrid-vertex"
# Agent Engine / Reasoning Engine is a regional resource; keep the session store
# in us-central1 alongside BigQuery/GCS. Models remain pinned to `global` in code
# (agent_common), so GOOGLE_CLOUD_LOCATION stays unset — see CLAUDE.md.
LOCATION = "us-central1"
DISPLAY_NAME = "trend-trawler-sessions"


def _resource_name(engine: object) -> str:
    """Best-effort fully-qualified resource name across SDK versions."""
    name = getattr(engine, "resource_name", None)
    if name:
        return name
    gca = getattr(engine, "gca_resource", None)
    if gca is not None and getattr(gca, "name", None):
        return gca.name
    raise RuntimeError(f"Could not determine resource name for {engine!r}")


def main() -> None:
    vertexai.init(project=PROJECT, location=LOCATION)

    for existing in agent_engines.list():
        if getattr(existing, "display_name", None) == DISPLAY_NAME:
            name = _resource_name(existing)
            print(f"Reusing existing session engine: {name}")
            print(f"SESSION_SERVICE_URI=agentengine://{name}")
            return

    engine = agent_engines.create(
        display_name=DISPLAY_NAME,
        description="Dedicated Agent Engine backing persistent ADK api_server sessions.",
    )
    name = _resource_name(engine)
    print(f"Created session engine: {name}")
    print(f"SESSION_SERVICE_URI=agentengine://{name}")


if __name__ == "__main__":
    main()
