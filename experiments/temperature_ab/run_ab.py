"""Drive the Gemini 3 temperature A/B: serial, counterbalanced runs over two arms.

Each arm is a tagged, no-traffic revision of the private ``trend-trawler-api``
(same image, env ``GEMINI_TEMPERATURE_PROFILE=tuned|default``). For every planned
cell this mints a proxy-SA ID token (P3: ``X-TT-User`` is trusted only with it),
seeds a session like the frontend, kicks off the async ``/runs`` job, polls it to
the terminal marker, scrapes the run's log window, and writes one JSON record to
``results/<arm>/<app>/<cell_id>.json``. A failed run is a record, never a crash;
cells whose record exists are skipped, so an interrupted batch resumes.

Usage (see experiments/README.md → temperature_ab):
    PYTHONPATH="$PWD" EXP_INVOKER_SA=tt-web-sa@<proj>.iam.gserviceaccount.com \\
      uv run python -m experiments.temperature_ab.run_ab \\
      --arms experiments/temperature_ab/arms.json \\
      --apps creative_agent,trend_scout --reps 10 --gap-secs 60 [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from experiments.creative_latency import run_trial
from experiments.creative_latency.parse_run import summarize_run, summary_to_dict

from . import fixtures, metrics

RESULTS_ROOT = Path(__file__).parent / "results"
INVOKER_SA = os.environ.get("EXP_INVOKER_SA", "")
# Cloud Logging ingestion lags a few seconds behind the run's last entries.
LOG_SETTLE_SECS = 15.0


@dataclass(frozen=True)
class Cell:
    arm: str
    app: str
    campaign: str
    rep: int

    @property
    def cell_id(self) -> str:
        return f"{self.app}-{self.campaign}-r{self.rep:02d}-{self.arm}"


def plan_cells(arms: list[str], apps: list[str], reps: int) -> list[Cell]:
    """Execution order: counterbalanced ABBA pairs over (app, campaign) blocks.

    Each rep visits every (app, campaign) block once; each visit is a pair that
    runs every arm once. Pair ``k`` (counted over the whole plan) runs ``arms``
    in the given order when ``k`` is even and reversed when odd (tuned→default,
    default→tuned, …), so neither arm always goes first and an order /
    quota-warm-up effect cancels instead of biasing the contrast. The block
    order rotates by one each rep, so with an even number of blocks a given
    (app, campaign) also alternates which arm goes first from rep to rep
    (balanced for even ``reps``). Blocks alternate apps, so slow drift over a
    multi-hour batch hits both apps, campaigns and arms alike. Every arm gets
    each (app, campaign) exactly ``reps`` times. Deterministic: same inputs,
    same order, and cell ids depend only on (app, campaign, rep, arm).
    """
    for app in apps:
        if app not in fixtures.APPS:
            raise ValueError(f"unsupported app {app!r}; expected {fixtures.APPS}")
    blocks = [(app, c.key) for c in fixtures.CAMPAIGNS for app in apps]
    cells: list[Cell] = []
    k = 0
    for rep in range(reps):
        shift = rep % len(blocks)
        for app, campaign in blocks[shift:] + blocks[:shift]:
            order = arms if k % 2 == 0 else list(reversed(arms))
            cells += [Cell(arm=a, app=app, campaign=campaign, rep=rep) for a in order]
            k += 1
    return cells


def record_path(results_root: Path, cell: Cell) -> Path:
    return results_root / cell.arm / cell.app / f"{cell.cell_id}.json"


def _poll(base_url, user_id, sid, token, *, app, tt_user):
    return run_trial.poll_to_terminal(
        base_url, user_id, sid, token, app=app, tt_user=tt_user
    )


@dataclass
class Deps:
    """The live calls, injectable so the driver is testable offline."""

    mint: Callable[[str, str], str] = run_trial.mint_proxy_id_token
    create_session: Callable[..., str] = run_trial.create_session
    start_run: Callable[..., dict] = run_trial.start_run
    poll: Callable[..., tuple] = field(default=_poll)
    replay: Callable[..., tuple] = run_trial.fetch_replay
    log_counts: Callable[[str, float, float], dict | None] = metrics.scrape_log_counts
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time


def _event_span(events: list[dict], state: dict) -> float | None:
    try:
        span = summarize_run(events, state).total_wall_s
    except Exception:  # noqa: BLE001 — a malformed log must not lose the record
        return None
    return span if span > 0 else None


def run_cell(
    cell: Cell,
    arm_cfg: dict,
    *,
    invoker_sa: str,
    user_id: str = fixtures.EXPERIMENT_USER_ID,
    log_settle_secs: float = 0.0,
    deps: Deps | None = None,
) -> dict:
    """Run one cell end to end and return its record (errors are recorded)."""
    deps = deps or Deps()
    campaign = fixtures.CAMPAIGNS_BY_KEY[cell.campaign]
    base_url = arm_cfg["base_url"].rstrip("/")
    audience = (arm_cfg.get("audience") or base_url).rstrip("/")
    revision = arm_cfg.get("revision", "")

    session_id: str | None = None
    events: list[dict] = []
    state: dict = {}
    status = "exception"
    error: str | None = None
    started = deps.clock()
    try:
        token = deps.mint(audience, invoker_sa)
        session_id = deps.create_session(
            base_url,
            user_id,
            token,
            state=fixtures.initial_state(cell.app, campaign),
            app=cell.app,
            tt_user=user_id,
        )
        deps.start_run(
            base_url,
            user_id,
            session_id,
            token,
            app=cell.app,
            message=fixtures.kickoff_message(cell.app, campaign),
            tt_user=user_id,
        )
        events, state, status, error = deps.poll(
            base_url, user_id, session_id, token, app=cell.app, tt_user=user_id
        )
    except TimeoutError as exc:
        status, error = "timeout", str(exc)
    except Exception as exc:  # noqa: BLE001 — a failed run is data
        status = "exception"
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    ended = deps.clock()

    if log_settle_secs > 0:
        deps.sleep(log_settle_secs)
    log_counts = deps.log_counts(revision, started, ended)

    harness_wall = ended - started
    span = _event_span(events, state)
    summary = None
    if events:
        try:
            summary = summary_to_dict(summarize_run(events, state))
        except Exception:  # noqa: BLE001
            summary = None
    return {
        "cell_id": cell.cell_id,
        "arm": cell.arm,
        "app": cell.app,
        "campaign": cell.campaign,
        "rep": cell.rep,
        "revision": revision,
        "tag": arm_cfg.get("tag"),
        "base_url": base_url,
        "user_id": user_id,
        "session_id": session_id,
        "status": status,
        "error": error,
        "started_at": started,
        "ended_at": ended,
        # Server-side event span when the log has one (no poll granularity),
        # else the harness's own elapsed time.
        "wall_s": span if span is not None else harness_wall,
        "harness_wall_s": harness_wall,
        "events_count": len(events),
        "metrics": metrics.state_metrics(cell.app, state),
        "log_counts": log_counts,
        "summary": summary,
        "state": state,
    }


def run_ab(
    arms: dict[str, dict],
    cells: list[Cell],
    *,
    results_root: Path = RESULTS_ROOT,
    gap_secs: float = 60.0,
    invoker_sa: str = INVOKER_SA,
    log_settle_secs: float = LOG_SETTLE_SECS,
    deps: Deps | None = None,
) -> list[Path]:
    """Run every cell without a record, serially; return the written paths."""
    deps = deps or Deps()
    missing = sorted({c.arm for c in cells} - set(arms))
    if missing:
        raise KeyError(f"cells reference arms missing from the arms file: {missing}")
    todo = [c for c in cells if not record_path(results_root, c).exists()]
    print(
        f"[ab] {len(cells)} cells planned, {len(cells) - len(todo)} already done, "
        f"{len(todo)} to run",
        flush=True,
    )
    written: list[Path] = []
    for i, cell in enumerate(todo):
        if i > 0 and gap_secs > 0:
            deps.sleep(gap_secs)
        print(f"[ab] {i + 1}/{len(todo)} {cell.cell_id}", flush=True)
        record = run_cell(
            cell,
            arms[cell.arm],
            invoker_sa=invoker_sa,
            log_settle_secs=log_settle_secs,
            deps=deps,
        )
        path = record_path(results_root, cell)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, default=str))
        tmp.replace(path)
        print(
            f"[ab]   status={record['status']} wall={record['wall_s']:.0f}s "
            f"session={record['session_id']} -> {path}",
            flush=True,
        )
        written.append(path)
    return written


def rehydrate(
    arms: dict[str, dict],
    *,
    results_root: Path = RESULTS_ROOT,
    invoker_sa: str = INVOKER_SA,
    deps: Deps | None = None,
) -> list[Path]:
    """Re-read each recorded session's full state and recompute its metrics.

    For records written before the driver fetched the final state (the api sends
    ``state`` only on a ``since=0`` replay): no new runs, one GET per record.
    """
    deps = deps or Deps()
    tokens: dict[str, str] = {}
    updated: list[Path] = []
    for path in sorted(results_root.glob("*/*/*.json")):
        record = json.loads(path.read_text())
        sid, arm = record.get("session_id"), record.get("arm")
        if not sid or arm not in arms:
            continue
        audience = arms[arm]["audience"]
        if audience not in tokens:
            tokens[audience] = deps.mint(audience, invoker_sa)
        events, state = deps.replay(
            record["base_url"],
            record["user_id"],
            sid,
            tokens[audience],
            app=record["app"],
            tt_user=record["user_id"],
        )
        if not state:
            print(f"[ab] no state for {record['cell_id']}; left as is", flush=True)
            continue
        record["state"] = state
        record["metrics"] = metrics.state_metrics(record["app"], state)
        if events:
            record["events_count"] = len(events)
            try:
                record["summary"] = summary_to_dict(summarize_run(events, state))
            except Exception:  # noqa: BLE001
                pass
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, default=str))
        tmp.replace(path)
        print(f"[ab] rehydrated {record['cell_id']}: {record['metrics']}", flush=True)
        updated.append(path)
    return updated


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--arms",
        required=True,
        help='JSON: {"<arm>": {"base_url","audience","revision","tag"}, ...}',
    )
    p.add_argument("--apps", default="creative_agent,trend_scout")
    p.add_argument(
        "--reps",
        type=int,
        default=10,
        help="runs per arm per app, rounded up to a multiple of the 2 campaigns",
    )
    p.add_argument("--gap-secs", type=float, default=60.0)
    p.add_argument("--dry-run", action="store_true", help="print the plan only")
    p.add_argument(
        "--rehydrate",
        action="store_true",
        help="re-read each recorded session's full state and recompute metrics",
    )
    p.add_argument("--results-root", type=Path, default=RESULTS_ROOT)
    p.add_argument(
        "--invoker-sa",
        default=INVOKER_SA,
        help="Proxy SA to impersonate (tt-web-sa; default $EXP_INVOKER_SA).",
    )
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    arms = json.loads(Path(args.arms).read_text())
    apps = [a.strip() for a in args.apps.split(",") if a.strip()]
    # --reps counts runs per arm per app; each rep covers both campaigns.
    reps_per_campaign = max(1, -(-args.reps // len(fixtures.CAMPAIGNS)))
    cells = plan_cells(list(arms), apps, reps_per_campaign)
    if args.dry_run:
        for i, cell in enumerate(cells, 1):
            done = record_path(args.results_root, cell).exists()
            print(f"{i:3d} {cell.cell_id}{'  (done)' if done else ''}")
        return
    if not args.invoker_sa:
        raise SystemExit(
            "--invoker-sa / $EXP_INVOKER_SA is required: the api trusts X-TT-User "
            "only with an ID token of the proxy SA (tt-web-sa)"
        )
    if args.rehydrate:
        rehydrate(arms, results_root=args.results_root, invoker_sa=args.invoker_sa)
        return
    run_ab(
        arms,
        cells,
        results_root=args.results_root,
        gap_secs=args.gap_secs,
        invoker_sa=args.invoker_sa,
    )


if __name__ == "__main__":
    main()
