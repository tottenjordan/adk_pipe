"""Cache each selected session's full state, read-only, to ``data/state/<sid>.json``.

Sessions live in the dedicated sessions Reasoning Engine named by
``SESSION_SERVICE_URI`` (``agentengine://<id>`` or
``agentengine://projects/<p>/locations/<l>/reasoningEngines/<id>``, the form
``deployment/async_app.py`` hands to ADK). Only ``get_session`` is called;
already-cached sessions are skipped. A missing session is logged and listed in
``data/missing.json`` — re-run ``select`` with ``--exclude`` to replace it.

Usage:
    PYTHONPATH="$PWD" SESSION_SERVICE_URI=agentengine://<id> \\
      uv run python -m experiments.grader_temperature.fetch
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .common import (
    APP_NAME,
    SELECTION_PATH,
    STATE_DIR,
    load_selection,
    write_json,
)

logger = logging.getLogger(__name__)
DEFAULT_LOCATION = "us-central1"


def parse_session_uri(
    uri: str, *, project: str = "", location: str = DEFAULT_LOCATION
) -> dict[str, str]:
    """``agentengine://…`` → ``VertexAiSessionService`` kwargs (pure).

    Mirrors ADK's agentengine factory: a bare id takes ``project`` /
    ``location``; a full resource name carries its own.
    """
    prefix = "agentengine://"
    if not uri.startswith(prefix) or not uri[len(prefix) :].strip("/"):
        raise ValueError(f"SESSION_SERVICE_URI must be agentengine://…, got {uri!r}")
    part = uri[len(prefix) :].strip("/")
    if "/" not in part:
        if not project:
            raise ValueError("a bare engine id needs GOOGLE_CLOUD_PROJECT")
        return {"project": project, "location": location, "agent_engine_id": part}
    bits = part.split("/")
    if not (
        len(bits) == 6
        and bits[0] == "projects"
        and bits[2] == "locations"
        and bits[4] == "reasoningEngines"
    ):
        raise ValueError(f"malformed agent engine resource name {part!r}")
    return {"project": bits[1], "location": bits[3], "agent_engine_id": bits[5]}


def build_service() -> Any:
    from google.adk.sessions import VertexAiSessionService

    kwargs = parse_session_uri(
        os.environ.get("SESSION_SERVICE_URI", ""),
        project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        location=os.environ.get("GCP_REGION", DEFAULT_LOCATION),
    )
    return VertexAiSessionService(**kwargs)


async def fetch_all(
    selection: Mapping[str, Any], service: Any, state_dir: Path = STATE_DIR
) -> dict[str, list[str]]:
    """Cache every selected session's state; ``{fetched, cached, missing}``."""
    out: dict[str, list[str]] = {"fetched": [], "cached": [], "missing": []}
    for s in selection["sessions"]:
        sid, user = s["session_id"], s["user_id"]
        path = state_dir / f"{sid}.json"
        if path.exists():
            out["cached"].append(sid)
            continue
        try:
            session = await service.get_session(
                app_name=APP_NAME, user_id=user, session_id=sid
            )
        except Exception as exc:  # a foreign/deleted session can raise
            logger.warning("Session %s unreadable: %s", sid, exc)
            session = None
        if session is None:
            logger.warning("Session %s (user %s) is missing", sid, user)
            out["missing"].append(sid)
            continue
        write_json(path, dict(session.state))
        out["fetched"].append(sid)
    return out


def main(argv: Sequence[str] | None = None, *, service: Any = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--selection", type=Path, default=SELECTION_PATH)
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    selection = load_selection(args.selection)
    report = asyncio.run(
        fetch_all(selection, service or build_service(), args.state_dir)
    )
    write_json(args.state_dir.parent / "missing.json", report["missing"])
    print(
        f"fetched {len(report['fetched'])}, cached {len(report['cached'])}, "
        f"missing {len(report['missing'])}"
    )
    if report["missing"]:
        print(
            "Missing sessions: "
            + ",".join(report["missing"])
            + "\nRe-run select with --exclude "
            + ",".join(report["missing"])
        )
    return report


if __name__ == "__main__":
    main()
