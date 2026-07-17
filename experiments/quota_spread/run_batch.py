"""Fire N *concurrent* creative_agent runs at one (arm, load) cell and record them.

This is the one capability the serial latency harness deliberately lacks: a batch
is N runs launched at the same time against a single tagged revision, so the
cross-caller quota contention we want to measure actually happens. All network +
parse primitives are reused from :mod:`experiments.creative_latency.run_trial`;
this module only adds the ThreadPoolExecutor fan-out and per-cell record shaping.

Pure core (``assemble_batch_records``) is unit-tested; the threaded driver
(``run_batch``) is the live path (integration/dry-run only), mirroring the
latency package's convention.

Usage (one batch, standalone):
    PYTHONPATH="$PWD" uv run python -m experiments.quota_spread.run_batch \\
        --base-url https://<tag>---<service>-<hash>-<region>.run.app \\
        --audience https://<service>-<hash>-<region>.run.app \\
        --arm global_3x --concurrency 5 --batch-id b1 --revision <revision>
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from experiments.creative_latency.parse_run import summarize_run, summary_to_dict
from experiments.creative_latency.run_trial import (
    INVOKER_SA,
    count_429s,
    create_session,
    mint_token,
    poll_to_terminal,
    start_run,
)

RESULTS_ROOT = Path(__file__).parent / "results"


def assemble_batch_records(
    *,
    arm: str,
    concurrency: int,
    batch_id: str,
    revision: str,
    per_run: list[dict],
) -> list[dict]:
    """Shape already-fetched per-run results into tidy cell records (pure).

    ``per_run`` items carry ``session_id``, ``status``, ``error``, ``count_429``,
    ``started_at``, ``ended_at``, ``state`` (final session state), and ``summary``
    (the :func:`summary_to_dict` output). The final ``state`` rides along so the
    free ``creative_eval`` quality harvest (Task 8) needs no extra calls.
    """
    records: list[dict] = []
    for r in per_run:
        summary = r.get("summary") or {}
        phase = summary.get("phase_wall_s") or {}
        records.append(
            {
                "arm": arm,
                "concurrency": concurrency,
                "batch_id": batch_id,
                "revision": revision,
                "session_id": r.get("session_id"),
                "status": r.get("status"),
                "error": r.get("error"),
                "started_at_epoch": r.get("started_at"),
                "ended_at_epoch": r.get("ended_at"),
                "count_429": r.get("count_429"),
                # research_s is the combined_research_pipeline span — the phase the
                # parallel-planner contention inflates (the H1 primary signal).
                "research_s": phase.get("research"),
                "visual_s": phase.get("visual"),
                "eval_s": phase.get("eval"),
                "total_s": summary.get("total_wall_s"),
                "exhaustion": summary.get("exhaustion") or [],
                "summary": summary,
                "state": r.get("state") or {},
            }
        )
    return records


def _one_run(
    *, base_url: str, arm: str, batch_id: str, index: int, revision: str, token: str
) -> dict:
    """Drive a single run to terminal and return its raw result dict.

    Any exception is caught and folded into an ``error`` result so one failed run
    never sinks the whole concurrent batch (the point is to observe contention,
    including runs that degrade).
    """
    user_id = f"exp_{arm}_{batch_id}_{index}_{int(time.time())}"
    try:
        session_id = create_session(base_url, user_id, token)
        started = time.time()
        start_run(base_url, user_id, session_id, token)
        events, state, status, error = poll_to_terminal(
            base_url, user_id, session_id, token
        )
        ended = time.time()
        summary = summarize_run(events, state)
        return {
            "session_id": session_id,
            "status": status,
            "error": error,
            "count_429": count_429s(revision, started, ended),
            "started_at": started,
            "ended_at": ended,
            "state": state,
            "summary": summary_to_dict(summary),
        }
    except Exception as exc:  # noqa: BLE001 — a failed run is data, not a stop
        return {
            "session_id": None,
            "status": "error",
            "error": repr(exc),
            "count_429": None,
            "started_at": None,
            "ended_at": None,
            "state": {},
            "summary": {},
        }


def write_batch_records(records: list[dict], arm: str, concurrency: int, batch_id: str) -> Path:
    """Write one JSON per run under ``results/<arm>/N<k>/<batch_id>/``; return the dir."""
    out_dir = RESULTS_ROOT / arm / f"N{concurrency}" / batch_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for rec in records:
        name = rec.get("session_id") or f"error_{records.index(rec)}"
        (out_dir / f"{name}.json").write_text(json.dumps(rec, indent=2, default=str))
    return out_dir


def run_batch(
    *,
    base_url: str,
    arm: str,
    concurrency: int,
    batch_id: str,
    revision: str,
    audience: str | None = None,
    invoker_sa: str = INVOKER_SA,
    write: bool = True,
) -> list[dict]:
    """Launch ``concurrency`` runs at once against ``base_url``; return cell records.

    One token is minted up front and shared across threads (read-only string).
    ``base_url`` is where requests are sent (the tag URL for an isolated revision);
    ``audience`` is the token audience = BASE service URL (required for a tag URL).
    """
    token = mint_token(audience or base_url, invoker_sa)
    print(
        f"[batch {batch_id}] arm={arm} N={concurrency} -> firing "
        f"{concurrency} concurrent run(s)",
        flush=True,
    )
    per_run: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [
            ex.submit(
                _one_run,
                base_url=base_url,
                arm=arm,
                batch_id=batch_id,
                index=i,
                revision=revision,
                token=token,
            )
            for i in range(concurrency)
        ]
        for fut in as_completed(futures):
            res = fut.result()
            per_run.append(res)
            print(
                f"[batch {batch_id}] run done status={res['status']} "
                f"session={res['session_id']}",
                flush=True,
            )

    records = assemble_batch_records(
        arm=arm,
        concurrency=concurrency,
        batch_id=batch_id,
        revision=revision,
        per_run=per_run,
    )
    if write:
        out_dir = write_batch_records(records, arm, concurrency, batch_id)
        print(f"[batch {batch_id}] wrote {len(records)} record(s) to {out_dir}", flush=True)
    return records


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", required=True, help="URL requests are sent to (tag URL).")
    p.add_argument("--audience", default=None, help="Token audience = BASE service URL.")
    p.add_argument("--arm", required=True, help="regional_25 | global_3x | global_altbucket")
    p.add_argument("--concurrency", type=int, required=True, help="runs to fire at once (N)")
    p.add_argument("--batch-id", required=True, help="unique id for this batch")
    p.add_argument("--revision", default="", help="Cloud Run revision serving this arm.")
    p.add_argument("--invoker-sa", default=INVOKER_SA, help="SA to impersonate (default $EXP_INVOKER_SA).")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    try:
        run_batch(
            base_url=args.base_url.rstrip("/"),
            arm=args.arm,
            concurrency=args.concurrency,
            batch_id=args.batch_id,
            revision=args.revision,
            audience=args.audience.rstrip("/") if args.audience else None,
            invoker_sa=args.invoker_sa,
        )
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise SystemExit(f"HTTP error driving batch: {exc}") from exc


if __name__ == "__main__":
    main()
