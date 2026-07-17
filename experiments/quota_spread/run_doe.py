"""Drive the full quota-spread DoE: blocked, interleaved cells over arms × loads.

Given the per-arm tagged-revision map, this fires one concurrent ``run_batch``
per (arm, load, rep) cell in an order that blocks temporal drift out of the arm
contrast (within each load block no arm runs twice in a row, and every arm gets
the same rep count), sleeping between batches so the shared per-minute Vertex
quota recovers. Writes each cell's records plus a ``manifest.json``.

``plan_cell_order`` is pure and unit-tested; ``run_doe`` is the live path.

Usage:
    PYTHONPATH="$PWD" EXP_INVOKER_SA=tt-web-sa@<proj>.iam.gserviceaccount.com \\
      uv run python -m experiments.quota_spread.run_doe --arm-map arms.json \\
      --loads 1 5 --reps 4 --cool-secs 120
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .run_batch import RESULTS_ROOT, INVOKER_SA, run_batch


def plan_cell_order(
    arms: list[str], loads: list[int], reps: int
) -> list[tuple[str, int, int]]:
    """Return the (arm, N, rep) execution order — blocked by load, arms interleaved.

    Within each load block the arms are round-robined once per rep
    (``arm0, arm1, …, arm0, arm1, …``), which for any >=2 distinct arms yields no
    two consecutive cells on the same arm — so drift within a block hits every
    arm equally and the arm contrast at each load stays unbiased. Deterministic:
    order derives only from the input lists, no randomness or wall-clock.
    """
    order: list[tuple[str, int, int]] = []
    for load in loads:
        for rep in range(reps):
            for arm in arms:
                order.append((arm, load, rep))
    return order


def run_doe(
    *,
    arm_map: dict[str, dict],
    loads: list[int],
    reps: int,
    cool_secs: float = 120.0,
    invoker_sa: str = INVOKER_SA,
) -> Path:
    """Execute every cell in :func:`plan_cell_order`; write records + a manifest.

    ``arm_map`` maps each arm to ``{"base_url", "audience", "revision", "tag"}``
    (each arm is its own tagged Cloud Run revision). Returns the manifest path.
    """
    arms = list(arm_map)
    order = plan_cell_order(arms, loads, reps)
    print(
        f"[doe] {len(order)} cells: arms={arms} loads={loads} reps={reps} "
        f"cool={cool_secs}s",
        flush=True,
    )

    cells: list[dict] = []
    for i, (arm, load, rep) in enumerate(order):
        cfg = arm_map[arm]
        batch_id = f"{arm}_N{load}_r{rep}"
        print(f"[doe] cell {i + 1}/{len(order)}: {batch_id}", flush=True)
        records = run_batch(
            base_url=cfg["base_url"].rstrip("/"),
            arm=arm,
            concurrency=load,
            batch_id=batch_id,
            revision=cfg.get("revision", ""),
            audience=(cfg.get("audience") or "").rstrip("/") or None,
            invoker_sa=invoker_sa,
        )
        cells.append(
            {
                "batch_id": batch_id,
                "arm": arm,
                "concurrency": load,
                "rep": rep,
                "revision": cfg.get("revision", ""),
                "tag": cfg.get("tag"),
                "n_records": len(records),
                "n_done": sum(1 for r in records if r.get("status") == "done"),
            }
        )
        # Cool between batches so the shared per-minute quota refills (skip the last).
        if i < len(order) - 1 and cool_secs > 0:
            print(f"[doe] cooling {cool_secs:.0f}s…", flush=True)
            time.sleep(cool_secs)

    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path = RESULTS_ROOT / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "arm_map": arm_map,
                "loads": loads,
                "reps": reps,
                "cool_secs": cool_secs,
                "cells": cells,
            },
            indent=2,
            default=str,
        )
    )
    print(f"[doe] wrote manifest {manifest_path}", flush=True)
    return manifest_path


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--arm-map",
        required=True,
        help='JSON file: {"<arm>": {"base_url","audience","revision","tag"}, …}',
    )
    p.add_argument("--loads", type=int, nargs="+", default=[1, 5], help="concurrency levels")
    p.add_argument("--reps", type=int, default=4, help="batches per (arm, load) cell")
    p.add_argument("--cool-secs", type=float, default=120.0, help="sleep between batches")
    p.add_argument("--invoker-sa", default=INVOKER_SA, help="SA to impersonate.")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    arm_map = json.loads(Path(args.arm_map).read_text())
    run_doe(
        arm_map=arm_map,
        loads=args.loads,
        reps=args.reps,
        cool_secs=args.cool_secs,
        invoker_sa=args.invoker_sa,
    )


if __name__ == "__main__":
    main()
