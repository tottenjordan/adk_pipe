"""Run N serial, spaced creative_agent trials for one config and aggregate.

Trials are run one at a time with an ``INTER_TRIAL_SECS`` gap so the shared,
project-wide Vertex quota (5 RPM pro / 2 RPM image) recovers between runs and
the trials don't self-contend — the contention IS the thing we're measuring, so
overlapping our own trials would corrupt the signal.

The aggregation (``aggregate_records``) is pure and unit-tested in
``tests/test_experiment_aggregate.py``; the trial execution is the live path
(delegates to ``run_trial.run_trial``).

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.creative_latency.run_experiment \\
        --base-url https://exp-baseline---trend-trawler-api-qqzji3hyoa-uc.a.run.app \\
        --audience https://trend-trawler-api-qqzji3hyoa-uc.a.run.app \\
        --config-name baseline --revision <rev> --tag exp-baseline --n 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from .run_trial import RESULTS_ROOT, run_trial

INTER_TRIAL_SECS = 90.0


def _stats(values: list[float]) -> dict | None:
    if not values:
        return None
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def aggregate_records(config_name: str, records: list[dict]) -> dict:
    """Aggregate per-trial JSON records into median/min/max per phase + total.

    Only ``status == "done"`` trials feed the numeric stats (a short error run
    must not drag the medians), but ``n_trials`` still counts every attempt.
    """
    done = [r for r in records if r.get("status") == "done"]
    summaries = [r.get("summary", {}) for r in done]

    totals = [s["total_wall_s"] for s in summaries if "total_wall_s" in s]

    phase_keys: set[str] = set()
    for s in summaries:
        phase_keys.update((s.get("phase_wall_s") or {}).keys())
    phase_wall_s = {
        phase: _stats(
            [(s.get("phase_wall_s") or {}).get(phase, 0.0) for s in summaries]
        )
        for phase in sorted(phase_keys)
    }

    call_keys: set[str] = set()
    for s in summaries:
        call_keys.update((s.get("model_calls") or {}).keys())
    model_calls = {
        phase: _stats([(s.get("model_calls") or {}).get(phase, 0) for s in summaries])
        for phase in sorted(call_keys)
    }

    http_values = [r["http_429_503"] for r in done if r.get("http_429_503") is not None]

    return {
        "config": config_name,
        "n_trials": len(records),
        "n_done": len(done),
        "total_wall_s": _stats(totals),
        "phase_wall_s": phase_wall_s,
        "model_calls": model_calls,
        "http_429_503": _stats(http_values),
        "sessions": [r.get("session_id") for r in records],
    }


def _print_table(agg: dict) -> None:
    print("\n" + "=" * 60)
    print(f"CONFIG: {agg['config']}  ({agg['n_done']}/{agg['n_trials']} done)")
    print("=" * 60)
    total = agg["total_wall_s"]
    if total:
        print(
            f"{'total_wall_s':<20} median={total['median']:>8.1f}  "
            f"min={total['min']:>8.1f}  max={total['max']:>8.1f}"
        )
    for phase, st in agg["phase_wall_s"].items():
        if st:
            print(
                f"  {phase:<18} median={st['median']:>8.1f}  "
                f"min={st['min']:>8.1f}  max={st['max']:>8.1f}"
            )
    if agg.get("http_429_503"):
        h = agg["http_429_503"]
        print(
            f"{'http_429_503':<20} median={h['median']}  min={h['min']}  max={h['max']}"
        )
    print("=" * 60 + "\n")


def run_experiment(
    *,
    base_url: str,
    audience: str | None,
    config_name: str,
    revision: str,
    tag: str | None,
    n: int,
) -> dict:
    """Run ``n`` spaced trials, aggregate, and write ``results/<config>/_summary.json``."""
    records: list[dict] = []
    for i in range(n):
        print(f"\n--- trial {i + 1}/{n} [{config_name}] ---", flush=True)
        try:
            path = run_trial(
                base_url=base_url,
                config_name=config_name,
                revision=revision,
                tag=tag,
                audience=audience,
            )
            records.append(json.loads(Path(path).read_text()))
        except Exception as exc:  # noqa: BLE001 — one bad trial shouldn't abort the set
            print(f"  trial {i + 1} FAILED: {exc}", flush=True)
        if i < n - 1:
            print(f"  sleeping {INTER_TRIAL_SECS:.0f}s for quota recovery…", flush=True)
            time.sleep(INTER_TRIAL_SECS)

    agg = aggregate_records(config_name, records)
    out_dir = RESULTS_ROOT / config_name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_summary.json").write_text(json.dumps(agg, indent=2, default=str))
    _print_table(agg)
    print(f"wrote {out_dir / '_summary.json'}")
    return agg


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", required=True, help="Request target (tag URL).")
    p.add_argument("--audience", default=None, help="Token audience (base URL).")
    p.add_argument("--config-name", required=True)
    p.add_argument("--revision", default="")
    p.add_argument("--tag", default=None)
    p.add_argument("--n", type=int, default=3, help="Number of trials (default 3).")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    run_experiment(
        base_url=args.base_url.rstrip("/"),
        audience=args.audience.rstrip("/") if args.audience else None,
        config_name=args.config_name,
        revision=args.revision,
        tag=args.tag,
        n=args.n,
    )


if __name__ == "__main__":
    main()
