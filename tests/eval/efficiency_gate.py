"""Efficiency regression gate over ``adk eval``'s informational metrics.

``adk eval`` always reports four reference-free efficiency metrics per eval case
(status INFORMATIONAL, per-invocation averages): ``token_usage_v1``,
``inference_call_count_v1``, ``tool_call_count_v1`` and ``invocation_duration_v1``
(seconds). They cannot be given thresholds in the eval config (ADK raises), and
``adk eval`` exits 0 even when cases FAIL — so this script is the CI signal.

It reads the newest ``<agent>/.adk/eval_history/*.evalset_result.json`` and,
per eval case:

* FAILS if the case's ``final_eval_status`` is not PASSED (baseline or not);
* FAILS if a gated metric exceeds its baseline by more than ``TOLERANCES``;
* WARNS if ``invocation_duration_v1`` exceeds its baseline by more than
  ``WARN_ONLY`` (latency is too noisy under the shared quota to gate on);
* notes (warning, not failure) a missing or zero baseline.

Baselines live in ``docs/baselines/eval_efficiency.json`` as
``{agent: {eval_id: {metric: value}}}``; refresh with ``--update-baseline``
(refuses when any case failed) and land the diff in a reviewed PR.

Usable both as ``python tests/eval/efficiency_gate.py`` and as the module
``tests.eval.efficiency_gate`` (it imports nothing from the repo).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from google.adk.evaluation.eval_metrics import EvalStatus
from google.adk.evaluation.eval_result import EvalSetResult

TOLERANCES: dict[str, float] = {
    "token_usage_v1": 0.25,
    "inference_call_count_v1": 0.30,
    "tool_call_count_v1": 0.30,
}
WARN_ONLY: dict[str, float] = {"invocation_duration_v1": 0.50}
METRICS: tuple[str, ...] = (*TOLERANCES, *WARN_ONLY)

HISTORY_GLOB = "*.evalset_result.json"


def extract_metrics(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return ``{eval_id: {metric: score, "_passed": bool}}`` from an EvalSetResult."""
    result = EvalSetResult.model_validate(raw)
    out: dict[str, dict[str, Any]] = {}
    for case in result.eval_case_results:
        metrics: dict[str, Any] = {
            m.metric_name: m.score
            for m in case.overall_eval_metric_results
            if m.metric_name in METRICS and m.score is not None
        }
        metrics["_passed"] = case.final_eval_status == EvalStatus.PASSED
        out[case.eval_id] = metrics
    return out


def _delta(current: float, base: float) -> float:
    return (current - base) / base


def _rows(
    current: dict[str, dict[str, Any]], baseline: dict[str, dict[str, float]]
) -> tuple[list[str], list[str], list[tuple[str, ...]]]:
    failures: list[str] = []
    warnings: list[str] = []
    rows: list[tuple[str, ...]] = []
    for eval_id in sorted(current):
        case = current[eval_id]
        if not case.get("_passed", False):
            failures.append(f"{eval_id}: final_eval_status is not PASSED")
            rows.append(
                (eval_id, "final_eval_status", "PASSED", "not PASSED", "", "FAIL")
            )
        case_base = baseline.get(eval_id)
        if case_base is None:
            warnings.append(f"{eval_id}: no baseline for this case (not gated)")
        for metric in METRICS:
            if metric not in case:
                continue
            cur = float(case[metric])
            base = None if case_base is None else case_base.get(metric)
            if base is None:
                if case_base is not None:
                    warnings.append(f"{eval_id}/{metric}: no baseline (not gated)")
                rows.append((eval_id, metric, "-", f"{cur:g}", "-", "no baseline"))
                continue
            base = float(base)
            if base <= 0:
                warnings.append(
                    f"{eval_id}/{metric}: baseline is {base:g}, ratio skipped "
                    f"(current {cur:g})"
                )
                rows.append((eval_id, metric, f"{base:g}", f"{cur:g}", "-", "skipped"))
                continue
            delta = _delta(cur, base)
            status = "ok"
            if metric in TOLERANCES and delta > TOLERANCES[metric]:
                status = "FAIL"
                failures.append(
                    f"{eval_id}/{metric}: {cur:g} vs baseline {base:g} "
                    f"({delta:+.1%} > +{TOLERANCES[metric]:.0%})"
                )
            elif metric in WARN_ONLY and delta > WARN_ONLY[metric]:
                status = "WARN"
                warnings.append(
                    f"{eval_id}/{metric}: {cur:g} vs baseline {base:g} "
                    f"({delta:+.1%} > +{WARN_ONLY[metric]:.0%}, warn-only)"
                )
            rows.append(
                (eval_id, metric, f"{base:g}", f"{cur:g}", f"{delta:+.1%}", status)
            )
    return failures, warnings, rows


def compare(
    current: dict[str, dict[str, Any]], baseline_for_agent: dict[str, dict[str, float]]
) -> tuple[list[str], list[str]]:
    """Compare extracted metrics against one agent's baseline -> (failures, warnings)."""
    failures, warnings, _ = _rows(current, baseline_for_agent)
    return failures, warnings


def _markdown(
    agent: str, rows: list[tuple[str, ...]], failures: list[str], warnings: list[str]
) -> str:
    lines = [
        f"### adk eval efficiency gate: `{agent}`",
        "",
        "| case | metric | baseline | current | delta % | status |",
        "|---|---|---|---|---|---|",
        *(f"| {' | '.join(r)} |" for r in rows),
        "",
    ]
    if failures:
        lines += ["**Failures:**", *(f"- {f}" for f in failures), ""]
    if warnings:
        lines += ["**Warnings:**", *(f"- {w}" for w in warnings), ""]
    if not failures:
        lines += ["Result: PASS", ""]
    else:
        lines += ["Result: FAIL", ""]
    return "\n".join(lines)


def _load_result(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    # Legacy ADK history files were double-encoded (a JSON string of JSON).
    if isinstance(data, str):
        data = json.loads(data)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent", required=True, help="agent (app) name")
    parser.add_argument(
        "--baseline",
        default="docs/baselines/eval_efficiency.json",
        help="baseline JSON {agent: {eval_id: {metric: value}}}",
    )
    parser.add_argument(
        "--history-dir",
        default=None,
        help="eval history dir (default: <agent>/.adk/eval_history)",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="write this run's metrics as the agent's baseline (refuses on failed cases)",
    )
    args = parser.parse_args(argv)

    history = Path(args.history_dir or Path(args.agent) / ".adk" / "eval_history")
    files = sorted(history.glob(HISTORY_GLOB), key=lambda p: p.stat().st_mtime)
    if not files:
        print(f"error: no {HISTORY_GLOB} under {history}", file=sys.stderr)
        return 2
    newest = files[-1]
    current = extract_metrics(_load_result(newest))

    baseline_path = Path(args.baseline)
    all_baselines: dict[str, Any] = (
        json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    )

    if args.update_baseline:
        failed = sorted(e for e, m in current.items() if not m.get("_passed", False))
        if failed:
            print(
                "error: refusing to update baseline; cases not PASSED: "
                + ", ".join(failed),
                file=sys.stderr,
            )
            return 1
        all_baselines[args.agent] = {
            eval_id: {k: v for k, v in m.items() if k != "_passed"}
            for eval_id, m in current.items()
        }
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(all_baselines, indent=2, sort_keys=True) + "\n"
        )
        print(f"updated {baseline_path} [{args.agent}] from {newest.name}")
        return 0

    failures, warnings, rows = _rows(current, all_baselines.get(args.agent, {}))
    report = _markdown(args.agent, rows, failures, warnings)
    print(f"source: {newest}\n")
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
