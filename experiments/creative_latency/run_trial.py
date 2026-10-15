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

Auth is portable: set ``$EXP_INVOKER_SA`` (or ``--invoker-sa``) to a service
account with ``roles/run.invoker`` on your backend, or leave it empty to use your
own ADC identity — nothing project-specific is hard-coded.

Per-user authz (P3): an api in ``USER_AUTHZ_MODE=enforce`` (the default) 401s
user-scoped routes without a trusted ``X-TT-User``. The request helpers take an
optional ``tt_user`` for that header, and ``mint_proxy_id_token`` mints the
matching proxy-SA ID token with an ``email`` claim
(``experiments/temperature_ab/run_ab.py`` uses both). Without ``tt_user`` the
helpers behave exactly as before (an api with ``TRUST_CLIENT_USER_ID=1``).

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.creative_latency.run_trial \\
        --base-url https://<service>-<hash>-<region>.run.app \\
        --config-name baseline --revision <revision> [--tag <tag>]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .fixtures import APP_NAME, CAMPAIGN_MESSAGE
from .parse_run import summarize_run, summary_to_dict

# Service account with roles/run.invoker on the backend to impersonate when
# minting the ID token. Override with $EXP_INVOKER_SA or --invoker-sa; leave it
# empty to use your own ADC identity (no impersonation) — nothing project-
# specific is baked in, so anyone can point the harness at their own backend.
INVOKER_SA = os.environ.get("EXP_INVOKER_SA", "")
RESULTS_ROOT = Path(__file__).parent / "results"
POLL_INTERVAL_S = 3.0
POLL_TIMEOUT_S = 1800.0  # 30 min hard ceiling for one creative_agent run
# Per-request socket timeout. Generous on purpose: a late-run poll returns the
# FULL session state (research briefs + ad copies + visual concepts + the eval
# report), which is large enough to routinely exceed a tight 60s read. Too tight
# a value spuriously fails a run that actually completed server-side.
HTTP_TIMEOUT_S = 180.0
TERMINAL_STATUSES = frozenset({"done", "error"})
# Header the P3 backend trusts as the caller's identity (runserver/authz.py
# TRUSTED_USER_HEADER), but only alongside an ID token of the proxy SA.
USER_HEADER = "X-TT-User"
# Indirection so tests can swap in a fake transport.
_urlopen = urllib.request.urlopen


def mint_token(audience: str, invoker_sa: str = INVOKER_SA) -> str:
    """Mint an ID token for ``audience`` (the BASE service URL).

    Impersonates ``invoker_sa`` when set; otherwise mints against the caller's
    own ADC identity.
    """
    cmd = ["gcloud", "auth", "print-identity-token", f"--audiences={audience}"]
    if invoker_sa:
        cmd += [f"--impersonate-service-account={invoker_sa}", "--include-email"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _impersonation_module():
    from google.auth import impersonated_credentials

    return impersonated_credentials


def _default_credentials():
    import google.auth

    return google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )


def _auth_request():
    from google.auth.transport.requests import Request

    return Request()


def mint_proxy_id_token(audience: str, proxy_sa: str) -> str:
    """Mint an ID token AS the proxy SA (``tt-web-sa``) that carries ``email``.

    Under per-user authz (P3, ``USER_AUTHZ_MODE=enforce``) the backend trusts
    ``X-TT-User`` only alongside a Google ID token whose ``email`` claim is
    ``TRUSTED_PROXY_SA`` with ``email_verified`` true and an ``aud`` in
    ``TRUSTED_PROXY_AUDIENCES`` (``runserver/authz.py`` ``verify_proxy_caller``).
    An impersonated ID token only carries ``email`` when asked for: google-auth's
    ``impersonated_credentials.IDTokenCredentials(target_credentials,
    target_audience=None, include_email=False, quota_project_id=None)`` sends
    ``"includeEmail": include_email`` to the IAM Credentials ``generateIdToken``
    call in ``refresh()`` (google-auth 2.61.0,
    ``google/auth/impersonated_credentials.py``), hence ``include_email=True``.
    The caller's ADC needs ``roles/iam.serviceAccountTokenCreator`` on the SA.
    """
    if not proxy_sa:
        raise ValueError("a proxy service account to impersonate is required")
    imp = _impersonation_module()
    source, _project = _default_credentials()
    target = imp.Credentials(
        source_credentials=source,
        target_principal=proxy_sa,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    id_creds = imp.IDTokenCredentials(
        target, target_audience=audience, include_email=True
    )
    id_creds.refresh(_auth_request())
    return id_creds.token


def _headers(token: str, user: str | None, *, json_body: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    if user:
        headers[USER_HEADER] = user
    return headers


def _post(
    url: str,
    body: dict,
    token: str,
    timeout: float = HTTP_TIMEOUT_S,
    *,
    user: str | None = None,
) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST", headers=_headers(token, user, json_body=True)
    )
    with _urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get(
    url: str, token: str, timeout: float = HTTP_TIMEOUT_S, *, user: str | None = None
) -> dict:
    req = urllib.request.Request(url, headers=_headers(token, user))
    with _urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _user_kwargs(tt_user: str | None) -> dict:
    """``{"user": tt_user}`` only when set, so pre-P3 callers are unchanged."""
    return {"user": tt_user} if tt_user else {}


def _seg(value: str) -> str:
    """Quote one path segment; "@" is a valid pchar, so emails stay readable."""
    return urllib.parse.quote(value, safe="@")


def create_session(
    base_url: str,
    user_id: str,
    token: str,
    *,
    state: dict | None = None,
    app: str = APP_NAME,
    tt_user: str | None = None,
) -> str:
    """Create a session via ADK canned CRUD; return its generated id.

    ``state`` seeds the initial session state (what the frontend's
    ``buildInitialState`` sends); ``tt_user`` adds the P3 ``X-TT-User`` header.
    """
    url = f"{base_url}/apps/{_seg(app)}/users/{_seg(user_id)}/sessions"
    session = _post(url, {"state": dict(state or {})}, token, **_user_kwargs(tt_user))
    return session["id"]


def start_run(
    base_url: str,
    user_id: str,
    session_id: str,
    token: str,
    *,
    app: str = APP_NAME,
    message: str = CAMPAIGN_MESSAGE,
    tt_user: str | None = None,
) -> dict:
    url = f"{base_url}/runs/{_seg(app)}"
    return _post(
        url,
        {"userId": user_id, "sessionId": session_id, "message": message},
        token,
        **_user_kwargs(tt_user),
    )


def fetch_replay(
    base_url: str,
    user_id: str,
    session_id: str,
    token: str,
    *,
    app: str = APP_NAME,
    tt_user: str | None = None,
) -> tuple[list[dict], dict]:
    """A finished session's full event log and state (one ``since=0`` replay)."""
    base = f"{base_url}/runs/{_seg(app)}/{_seg(user_id)}/{_seg(session_id)}"
    payload = _get(f"{base}?since=0", token, **_user_kwargs(tt_user))
    return payload.get("events") or [], payload.get("state") or {}


def _final_state(base: str, token: str, tt_user: str | None) -> dict | None:
    """The session's full state from a ``since=0`` replay, or None if it fails."""
    for _ in range(3):
        try:
            payload = _get(f"{base}?since=0", token, **_user_kwargs(tt_user))
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            print(f"  [poll] final state read failed, retrying: {exc!r}", flush=True)
            time.sleep(POLL_INTERVAL_S)
            continue
        return payload.get("state") or None
    return None


def poll_to_terminal(
    base_url: str,
    user_id: str,
    session_id: str,
    token: str,
    *,
    app: str = APP_NAME,
    tt_user: str | None = None,
    timeout_s: float | None = None,
) -> tuple[list[dict], dict, str, str | None]:
    """Accumulate events via the ``nextCursor`` until a terminal status.

    Returns ``(events, state, status, error)``. Raises ``TimeoutError`` if the
    run doesn't reach a terminal marker within ``timeout_s`` (default
    ``POLL_TIMEOUT_S``).
    """
    limit = POLL_TIMEOUT_S if timeout_s is None else timeout_s
    events: list[dict] = []
    since = 0
    state: dict = {}
    status = "running"
    error: str | None = None
    deadline = time.monotonic() + limit
    base = f"{base_url}/runs/{_seg(app)}/{_seg(user_id)}/{_seg(session_id)}"
    while time.monotonic() < deadline:
        try:
            payload = _get(f"{base}?since={since}", token, **_user_kwargs(tt_user))
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            # A single slow/failed poll must NOT sink a run that is still
            # progressing server-side (the detached task is decoupled from this
            # HTTP read). Log it and retry until the deadline; only a genuine
            # non-terminal stall out to POLL_TIMEOUT_S raises below.
            print(f"  [poll] transient read error, retrying: {exc!r}", flush=True)
            time.sleep(POLL_INTERVAL_S)
            continue
        status = payload.get("status", "running")
        new_events = payload.get("events") or []
        if new_events:
            events.extend(new_events)
            since = payload.get("nextCursor", since + len(new_events))
        state = payload.get("state") or state
        error = payload.get("error")
        if status in TERMINAL_STATUSES:
            if "state" not in payload:
                # The api returns ``state`` only on a full replay (since=0), so a
                # terminal incremental poll is re-read once for the final state.
                state = _final_state(base, token, tt_user) or state
            return events, state, status, error
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(
        f"run {session_id} did not finish within {limit:.0f}s "
        f"(last status={status}, {len(events)} events)"
    )


def _iso(epoch: float) -> str:
    """Epoch seconds -> RFC3339 UTC string gcloud logging filters accept."""
    return (
        datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


QUOTA_SIGNALS = (
    "httpRequest.status=429 OR httpRequest.status=503 "
    'OR textPayload=~"429" OR textPayload=~"503" '
    'OR textPayload=~"RESOURCE_EXHAUSTED" OR textPayload=~"UNAVAILABLE" '
    'OR jsonPayload.message=~"429" OR jsonPayload.message=~"RESOURCE_EXHAUSTED"'
)


def _build_log_filter(
    *,
    revision: str,
    start_epoch: float,
    end_epoch: float,
    signals: str = QUOTA_SIGNALS,
) -> str:
    """Build the Cloud Logging filter for ``signals`` in a run's window.

    ``signals`` defaults to ``QUOTA_SIGNALS``; other harnesses pass their
    own OR-expression (``experiments/temperature_ab/metrics.py``).

    Covers BOTH the HTTP-level status (Cloud Run request logs) AND the in-app
    model-error text (ADK retry warnings for Vertex 429/503), since a model 429
    absorbed by infra retry never surfaces as a Cloud Run ``httpRequest.status``.
    Returns ``""`` when ``revision`` is empty (nothing safe to scope to).
    """
    if not revision:
        return ""
    return (
        'resource.type="cloud_run_revision" '
        f'AND resource.labels.revision_name="{revision}" '
        f'AND timestamp>="{_iso(start_epoch)}" '
        f'AND timestamp<="{_iso(end_epoch)}" '
        f"AND ({signals})"
    )


def count_429s(revision: str, start_epoch: float, end_epoch: float) -> int | None:
    """Best-effort count of quota-signal log entries in the run window.

    Non-blocking: returns ``None`` on ANY error (missing revision, gcloud not
    installed, permission denied, bad JSON) so timing — the primary signal — is
    never held hostage to log access.
    """
    log_filter = _build_log_filter(
        revision=revision, start_epoch=start_epoch, end_epoch=end_epoch
    )
    if not log_filter:
        return None
    try:
        out = subprocess.run(
            [
                "gcloud",
                "logging",
                "read",
                log_filter,
                "--format=json",
                "--limit=1000",
                "--freshness=2h",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        return len(json.loads(out.stdout or "[]"))
    except Exception:  # noqa: BLE001 — enrichment only; degrade to None
        return None


def build_record(
    *,
    config_name: str,
    tag: str | None,
    revision: str,
    user_id: str,
    session_id: str,
    started_at: float | None,
    status: str,
    error: str | None,
    http_429_503: int | None,
    summary,
) -> dict:
    """Assemble the on-disk trial record (shared by the live + recovery paths)."""
    return {
        "config": config_name,
        "tag": tag,
        "revision": revision,
        "app_name": APP_NAME,
        "user_id": user_id,
        "session_id": session_id,
        "started_at_epoch": started_at,
        "status": status,
        "error": error,
        "http_429_503": http_429_503,
        "events_count": summary.event_count,
        "summary": summary_to_dict(summary),
    }


def write_record(record: dict, config_name: str, session_id: str) -> Path:
    """Write one trial record to ``results/<config>/<session_id>.json``."""
    out_dir = RESULTS_ROOT / config_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{session_id}.json"
    out_path.write_text(json.dumps(record, indent=2, default=str))
    return out_path


def run_trial(
    *,
    base_url: str,
    config_name: str,
    revision: str,
    tag: str | None,
    audience: str | None = None,
    invoker_sa: str = INVOKER_SA,
) -> Path:
    """Execute one trial end-to-end and write its JSON record. Returns the path.

    ``base_url`` is where requests are SENT (for an isolated tagged revision this
    is the tag URL ``https://<tag>---<svc>...run.app``). ``audience`` is the token
    audience, which for a Cloud Run tag must be the BASE service URL, not the tag
    URL; it defaults to ``base_url`` when the run targets the untagged service.
    ``invoker_sa`` is the service account to impersonate (empty = own identity).
    """
    token = mint_token(audience or base_url, invoker_sa)
    user_id = f"exp_{config_name}_{int(time.time())}"
    session_id = create_session(base_url, user_id, token)
    started_at = time.time()
    print(f"  session={session_id} user={user_id} -> starting run", flush=True)
    start_run(base_url, user_id, session_id, token)

    events, state, status, error = poll_to_terminal(
        base_url, user_id, session_id, token
    )
    ended_at = time.time()
    elapsed = ended_at - started_at
    summary = summarize_run(events, state)
    http_429_503 = count_429s(revision, started_at, ended_at)
    print(
        f"  done status={status} wall={summary.total_wall_s:.1f}s "
        f"(harness elapsed {elapsed:.1f}s) events={summary.event_count}",
        flush=True,
    )

    record = build_record(
        config_name=config_name,
        tag=tag,
        revision=revision,
        user_id=user_id,
        session_id=session_id,
        started_at=started_at,
        status=status,
        error=error,
        http_429_503=http_429_503,
        summary=summary,
    )
    out_path = write_record(record, config_name, session_id)
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
    p.add_argument(
        "--invoker-sa",
        default=INVOKER_SA,
        help="Service account to impersonate for the ID token "
        "(default $EXP_INVOKER_SA; empty = your own identity).",
    )
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
            invoker_sa=args.invoker_sa,
        )
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise SystemExit(f"HTTP error driving run: {exc}") from exc


if __name__ == "__main__":
    main()
