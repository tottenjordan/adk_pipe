"""Create (or reuse) the dedicated Agent Engine that backs persistent ADK sessions.

The Cloud Run backend runs the `deployment/async_app.py` launcher under uvicorn,
which reads `SESSION_SERVICE_URI=agentengine://<resource>` and builds a
`VertexAiSessionService`. That service
stores sessions *inside* a Reasoning Engine (Agent Engine). We give it a
**dedicated** engine — `trend-trawler-sessions` — that serves no agent, so the
session store's lifetime is decoupled from any served-agent deploy.

The engine is created via `agentplatform.Client().runtimes.create` with no
`agent`/`runtime` payload (the SDK allows a bare container) in `us-central1`,
which pins the session store to the region while the gemini-3.x models stay
pinned to `global` in code. The printed, fully-qualified
resource name is what ships as `SESSION_SERVICE_URI=agentengine://<resource>`.

Idempotent: reuses an existing engine with the same display name if one exists.

Project and region come from CLI flags, falling back to the repo `.env` /
environment (`GOOGLE_CLOUD_PROJECT`, `GCP_REGION`); the project is required.

Usage:
    uv run python deployment/create_session_engine.py
    uv run python deployment/create_session_engine.py --project <PROJECT_ID> --region us-central1
"""

from __future__ import annotations

import argparse
import os

import agentplatform
import dotenv

ENV_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Agent Engine / Reasoning Engine is a regional resource; keep the session store
# in GCP_REGION (us-central1) alongside BigQuery/GCS. Models remain pinned to
# `global` in code (agent_common), so GOOGLE_CLOUD_LOCATION is not used here —
# see CLAUDE.md.
DEFAULT_REGION = "us-central1"
DEFAULT_DISPLAY_NAME = "trend-trawler-sessions"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args; defaults are read from the environment at call time."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project",
        default=os.getenv("GOOGLE_CLOUD_PROJECT"),
        help="GCP project ID (default: $GOOGLE_CLOUD_PROJECT).",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("GCP_REGION", DEFAULT_REGION),
        help=f"Agent Engine region (default: $GCP_REGION or {DEFAULT_REGION}).",
    )
    parser.add_argument(
        "--display_name",
        default=DEFAULT_DISPLAY_NAME,
        help=f"Session engine display name (default: {DEFAULT_DISPLAY_NAME}).",
    )
    args = parser.parse_args(argv)
    if not args.project:
        parser.error("no project: pass --project or set GOOGLE_CLOUD_PROJECT")
    return args


def find_engine(client, display_name: str) -> str | None:
    """Return the resource name of an existing engine with `display_name`."""
    for existing in client.runtimes.list():
        if existing.api_resource.display_name == display_name:
            return existing.api_resource.name
    return None


def create_or_reuse(client, display_name: str) -> tuple[str, bool]:
    """Reuse the named engine if it exists, else create a sessions-only one.

    Returns `(resource_name, created)`. `create()` with no `agent` builds a
    lightweight engine that serves nothing — exactly what a session store needs.
    """
    name = find_engine(client, display_name)
    if name:
        return name, False
    engine = client.runtimes.create(
        config={
            "display_name": display_name,
            "description": (
                "Dedicated Agent Engine backing persistent ADK api_server sessions."
            ),
        }
    )
    if not engine.api_resource:
        raise RuntimeError("runtimes.create returned no resource")
    return engine.api_resource.name, True


def main(argv: list[str] | None = None) -> None:
    dotenv.load_dotenv(dotenv_path=ENV_FILE_PATH)
    args = parse_args(argv)
    client = agentplatform.Client(
        project=args.project,
        location=args.region,
    )  # pyright: ignore[reportCallIssue]
    name, created = create_or_reuse(client, args.display_name)
    verb = "Created" if created else "Reusing existing"
    print(f"{verb} session engine: {name}")
    print(f"SESSION_SERVICE_URI=agentengine://{name}")


if __name__ == "__main__":
    main()
