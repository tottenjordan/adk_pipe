"""Drive ONE live creative_agent run through the async ``/runs`` API and record
its per-phase latency breakdown as JSON.

This is the live network path of the harness (no unit test, mirroring the repo
convention for live scripts — the pure parsing it delegates to is tested in
``tests/test_experiment_parse.py``). It:

  1. mints an impersonated ID token for the private Cloud Run backend,
  2. creates a session (ADK canned CRUD),
  3. POSTs to ``/runs/{app}`` to kick off a detached run,
  4. polls ``GET /runs/{app}/{user}/{sid}?since=N`` to the terminal marker,
  5. summarizes the event log (``parse_run.summarize_run``), and
  6. writes ``results/<config>/<session_id>.json``.

Auth recipe (private tagged revision): impersonate ``tt-web-sa`` with the
audience set to the BASE service URL (NOT the tag URL). See the module CLI help.

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.creative_latency.run_trial \\
        --base-url https://trend-trawler-api-qqzji3hyoa-uc.a.run.app \\
        --config-name baseline --revision trend-trawler-api-00040-abc \\
        [--tag exp-baseline]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .fixtures import APP_NAME, CAMPAIGN_MESSAGE
from .parse_run import summarize_run

# Service account the same-origin proxy impersonates in prod (roles/run.invoker).
INVOKER_SA = "tt-web-sa@hybrid-vertex.iam.gserviceaccount.com"
RESULTS_ROOT = Path(__file__).parent / "results"
POLL_INTERVAL_S = 3.0
POLL_TIMEOUT_S = 1800.0  # 30 min hard ceiling for one creative_agent run
TERMINAL_STATUSES = frozenset({"done", "error"})


def mint_token(audience: str) -> str:
    """Mint an impersonated ID token for ``audience`` (the BASE service URL)."""
    out = subprocess.run(
        [
            "gcloud",
            "auth",
            "print-identity-token",
            f"--impersonate-service-account={INVOKER_SA}",
            f"--audiences={audience}",
            "--include-email",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _post(url: str, body: dict, token: str) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def create_session(base_url: str, user_id: str, token: str) -> str:
    """Create a session via ADK canned CRUD; return its generated id."""
    url = f"{base_url}/apps/{APP_NAME}/users/{user_id}/sessions"
    session = _post(url, {"state": {}}, token)
    return session["id"]


def start_run(base_url: str, user_id: str, session_id: str, token: str) -> dict:
    url = f"{base_url}/runs/{APP_NAME}"
    return _post(
        url,
        {"userId": user_id, "sessionId": session_id, "message": CAMPAIGN_MESSAGE},
        token,
    )


def poll_to_terminal(
    base_url: str, user_id: str, session_id: str, token: str
) -> tuple[list[dict], dict, str, str | None]:
    """Accumulate events via the ``nextCursor`` until a terminal status.

    Returns ``(events, state, status, error)``. Raises ``TimeoutError`` if the
    run doesn't reach a terminal marker within ``POLL_TIMEOUT_S``.
    """
    events: list[dict] = []
    since = 0
    state: dict = {}
    status = "running"
    error: str | None = None
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        base = f"{base_url}/runs/{APP_NAME}/{user_id}/{session_id}"
        payload = _get(f"{base}?since={since}", token)
        status = payload.get("status", "running")
        new_events = payload.get("events") or []
        if new_events:
            events.extend(new_events)
            since = payload.get("nextCursor", since + len(new_events))
        state = payload.get("state") or state
        error = payload.get("error")
        if status in TERMINAL_STATUSES:
            return events, state, status, error
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(
        f"run {session_id} did not finish within {POLL_TIMEOUT_S:.0f}s "
        f"(last status={status}, {len(events)} events)"
    )


def run_trial(
    *,
    base_url: str,
    config_name: str,
    revision: str,
    tag: str | None,
    audience: str | None = None,
) -> Path:
    """Execute one trial end-to-end and write its JSON record. Returns the path.

    ``base_url`` is where requests are SENT (for an isolated tagged revision this
    is the tag URL ``https://<tag>---<svc>...run.app``). ``audience`` is the token
    audience, which for a Cloud Run tag must be the BASE service URL, not the tag
    URL; it defaults to ``base_url`` when the run targets the untagged service.
    """
    token = mint_token(audience or base_url)
    user_id = f"exp_{config_name}_{int(time.time())}"
    session_id = create_session(base_url, user_id, token)
    started_at = time.time()
    print(f"  session={session_id} user={user_id} -> starting run", flush=True)
    start_run(base_url, user_id, session_id, token)

    events, state, status, error = poll_to_terminal(
        base_url, user_id, session_id, token
    )
    elapsed = time.time() - started_at
    summary = summarize_run(events, state)
    print(
        f"  done status={status} wall={summary.total_wall_s:.1f}s "
        f"(harness elapsed {elapsed:.1f}s) events={summary.event_count}",
        flush=True,
    )

    record = {
        "config": config_name,
        "tag": tag,
        "revision": revision,
        "app_name": APP_NAME,
        "user_id": user_id,
        "session_id": session_id,
        "started_at_epoch": started_at,
        "status": status,
        "error": error,
        "events_count": summary.event_count,
        "summary": dataclasses.asdict(summary),
    }
    out_dir = RESULTS_ROOT / config_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{session_id}.json"
    out_path.write_text(json.dumps(record, indent=2, default=str))
    print(f"  wrote {out_path}", flush=True)
    return out_path


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--base-url",
        required=True,
        help="URL requests are sent to (the tag URL for an isolated revision).",
    )
    p.add_argument(
        "--audience",
        default=None,
        help="Token audience = BASE service URL. Defaults to --base-url when the "
        "target is the untagged service (a tag URL needs the base URL here).",
    )
    p.add_argument("--config-name", required=True, help="e.g. baseline, fewer_pro")
    p.add_argument(
        "--revision", default="", help="Cloud Run revision serving this config."
    )
    p.add_argument("--tag", default=None, help="Cloud Run traffic tag, if any.")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    try:
        run_trial(
            base_url=args.base_url.rstrip("/"),
            config_name=args.config_name,
            revision=args.revision,
            tag=args.tag,
            audience=args.audience.rstrip("/") if args.audience else None,
        )
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise SystemExit(f"HTTP error driving run: {exc}") from exc


if __name__ == "__main__":
    main()
