"""Recover trial records from already-completed, persisted runs (no new compute).

The async ``/runs`` architecture persists every event + the terminal
``__run_status`` marker to the shared ``VertexAiSessionService``, decoupled from
the HTTP request and the serving revision. So when a live trial's run *succeeds*
but the harness fails to write its JSON locally (e.g. a serialization bug), the
run is NOT lost: re-poll the same ``user_id/session_id`` and re-summarize — zero
extra quota, no re-run of the agent.

Give it the ``user_id:session_id`` pairs printed by ``run_experiment`` (one per
``--pair``); it writes the same ``results/<config>/<session_id>.json`` a healthy
live trial would have, so ``plot.py`` / ``aggregate_records`` just work.

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.creative_latency.recover_trials \\
        --base-url https://<tag>---<service>-<hash>-<region>.run.app \\
        --audience https://<service>-<hash>-<region>.run.app \\
        --config-name baseline --revision <revision> --tag <tag> \\
        --pair <user_id>:<session_id> \\
        --pair <user_id>:<session_id>
"""

from __future__ import annotations

import argparse
import urllib.error

from .parse_run import summarize_run
from .run_trial import (
    INVOKER_SA,
    build_record,
    count_429s,
    mint_token,
    poll_to_terminal,
    write_record,
)


def recover_trial(
    *,
    base_url: str,
    audience: str | None,
    config_name: str,
    revision: str,
    tag: str | None,
    user_id: str,
    session_id: str,
    invoker_sa: str = INVOKER_SA,
) -> str:
    """Re-poll one persisted session and write its record. Returns the out path."""
    token = mint_token(audience or base_url, invoker_sa)
    events, state, status, error = poll_to_terminal(
        base_url, user_id, session_id, token
    )
    summary = summarize_run(events, state)
    started_at = summary.started_at_epoch
    end_at = (started_at + summary.total_wall_s) if started_at is not None else None
    http_429_503 = (
        count_429s(revision, started_at, end_at)
        if started_at is not None and end_at is not None
        else None
    )
    print(
        f"  {session_id} status={status} wall={summary.total_wall_s:.1f}s "
        f"events={summary.event_count}",
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
    return str(out_path)


def _parse_pair(raw: str) -> tuple[str, str]:
    user_id, _, session_id = raw.partition(":")
    if not user_id or not session_id:
        raise argparse.ArgumentTypeError(f"expected user_id:session_id, got {raw!r}")
    return user_id, session_id


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", required=True, help="Request target (tag URL).")
    p.add_argument("--audience", default=None, help="Token audience (base URL).")
    p.add_argument("--config-name", required=True)
    p.add_argument("--revision", default="")
    p.add_argument("--tag", default=None)
    p.add_argument(
        "--invoker-sa",
        default=INVOKER_SA,
        help="Service account to impersonate for the ID token "
        "(default $EXP_INVOKER_SA; empty = your own identity).",
    )
    p.add_argument(
        "--pair",
        action="append",
        type=_parse_pair,
        required=True,
        metavar="USER_ID:SESSION_ID",
        help="A user_id:session_id to recover (repeatable).",
    )
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    base_url = args.base_url.rstrip("/")
    audience = args.audience.rstrip("/") if args.audience else None
    config = args.config_name
    for user_id, session_id in args.pair:
        print(f"\n--- recover {config} :: {session_id} ---", flush=True)
        try:
            recover_trial(
                base_url=base_url,
                audience=audience,
                config_name=config,
                revision=args.revision,
                tag=args.tag,
                user_id=user_id,
                session_id=session_id,
                invoker_sa=args.invoker_sa,
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  recovery FAILED for {session_id}: {exc}", flush=True)


if __name__ == "__main__":
    main()
