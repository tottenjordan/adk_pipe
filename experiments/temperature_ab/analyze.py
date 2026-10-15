"""Analyse the temperature A/B records against the PRE-REGISTERED decision rule.

Reads ``results/<arm>/<app>/<cell_id>.json`` (written by ``run_ab``), reports per
app and metric n / mean / median per arm and ``default − tuned`` with a seeded
95% percentile-bootstrap CI, and evaluates the rule below. Writes
``summary.md`` + ``records.csv`` next to the records. Pure and offline.

The rule (fixed before any live run; arms must be named ``tuned`` and
``default``; every diff is ``default − tuned``, compared on point estimates —
the CIs are reported for context, not used by the rule):

1. completion: failed/unfinished runs(default) <= failed(tuned) + 1
2. robustness: mean abnormal+empty turns per run, mean ValidationErrors per run
   and mean retry_exhausted markers per run — each default <= tuned
3. creative quality: gates_pass_rate diff >= -0.05; mean_score diff >= -0.03;
   n_issues diff <= +1
4. creative diversity: headline_jaccard diff <= +0.05
5. trend_scout stability: classifier agreement(default) >= agreement(tuned)
6. latency: median wall time(default) <= 1.2 x median(tuned), per app, over
   completed runs

Each check is PASS / FAIL / N/A (no data on a side). Overall: FAIL if any check
fails, else INCONCLUSIVE if any is N/A, else PASS.

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.temperature_ab.analyze
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import random
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .metrics import LOG_COUNT_KEYS

TUNED, DEFAULT = "tuned", "default"
ARMS = (TUNED, DEFAULT)
APPS = ("creative_agent", "trend_scout")
N_RESAMPLES = 10_000
SEED = 20261015
# Float tolerance for the threshold comparisons (0.75 - 0.8 != -0.05 exactly).
EPS = 1e-9
RESULTS_ROOT = Path(__file__).parent / "results"

# Thresholds of the pre-registered rule.
MAX_EXTRA_FAILURES = 1
MIN_GATES_DIFF = -0.05
MIN_SCORE_DIFF = -0.03
MAX_ISSUES_DIFF = 1.0
MAX_JACCARD_DIFF = 0.05
MAX_LATENCY_RATIO = 1.2


@dataclass(frozen=True)
class BootstrapResult:
    diff: float
    lo: float
    hi: float


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # PASS | FAIL | N/A
    detail: str


def bootstrap_diff(
    default_vals: list[float],
    tuned_vals: list[float],
    *,
    n_resamples: int = N_RESAMPLES,
    seed: int = SEED,
) -> BootstrapResult | None:
    """``mean(default) − mean(tuned)`` with a 95% percentile-bootstrap CI.

    Each arm is resampled independently with replacement; seeded, so the same
    inputs always give the same interval. ``None`` when either side is empty.
    """
    if not default_vals or not tuned_vals:
        return None
    rng = random.Random(seed)
    diff = statistics.fmean(default_vals) - statistics.fmean(tuned_vals)
    nd, nt = len(default_vals), len(tuned_vals)
    diffs = sorted(
        statistics.fmean(rng.choices(default_vals, k=nd))
        - statistics.fmean(rng.choices(tuned_vals, k=nt))
        for _ in range(n_resamples)
    )
    lo = diffs[int(0.025 * (n_resamples - 1))]
    hi = diffs[int(round(0.975 * (n_resamples - 1)))]
    return BootstrapResult(diff=diff, lo=lo, hi=hi)


# --- record helpers ----------------------------------------------------------


def load_records(root: Path) -> list[dict]:
    """Every ``<arm>/<app>/<cell_id>.json`` record under ``root``."""
    return [json.loads(p.read_text()) for p in sorted(root.glob("*/*/*.json"))]


def _metrics(record: dict) -> dict:
    m = record.get("metrics")
    return m if isinstance(m, dict) else {}


def run_ok(record: dict) -> bool:
    """A run counts as completed: status done and the app's completion signal."""
    if record.get("status") != "done":
        return False
    m = _metrics(record)
    if record.get("app") == "creative_agent":
        return bool(m.get("finalized"))
    return bool(m.get("completed"))


def _log(record: dict, key: str) -> float | None:
    counts = record.get("log_counts")
    if not isinstance(counts, dict) or counts.get(key) is None:
        return None
    return float(counts[key])


def _turns(record: dict) -> float | None:
    abnormal, empty = _log(record, "abnormal_turns"), _log(record, "empty_turns")
    if abnormal is None or empty is None:
        return None
    return abnormal + empty


def _metric(key: str) -> Callable[[dict], float | None]:
    def get(record: dict) -> float | None:
        value = _metrics(record).get(key)
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, int | float):
            return float(value)
        return None

    return get


def _wall(record: dict) -> float | None:
    value = record.get("wall_s")
    return float(value) if isinstance(value, int | float) else None


def _logkey(key: str) -> Callable[[dict], float | None]:
    return lambda r: _log(r, key)


_COMMON: list[tuple[str, Callable[[dict], float | None]]] = [
    ("n_retry_exhausted", _metric("n_retry_exhausted")),
    ("wall_s", _wall),
    ("turns", _turns),
    *[(key, _logkey(key)) for key in LOG_COUNT_KEYS],
]
METRICS: dict[str, list[tuple[str, Callable[[dict], float | None]]]] = {
    "creative_agent": [
        (k, _metric(k))
        for k in (
            "finalized",
            "gates_pass_rate",
            "mean_score",
            "pass_rate",
            "n_issues",
            "image_qa_attempts",
            "headline_jaccard",
        )
    ]
    + _COMMON,
    "trend_scout": [
        (k, _metric(k)) for k in ("completed", "n_selected", "info_gtrends_exhausted")
    ]
    + _COMMON,
}


def _values(
    records: Iterable[dict], arm: str, getter: Callable[[dict], float | None]
) -> list[float]:
    out = []
    for r in records:
        if r.get("arm") != arm:
            continue
        value = getter(r)
        if value is not None:
            out.append(value)
    return out


def _of_app(records: Iterable[dict], app: str) -> list[dict]:
    return [r for r in records if r.get("app") == app]


def metric_table(
    records: list[dict], *, n_resamples: int = N_RESAMPLES, seed: int = SEED
) -> list[dict]:
    """One row per (app, metric): n / mean / median per arm + the bootstrap diff."""
    rows = []
    for app in APPS:
        app_records = _of_app(records, app)
        if not app_records:
            continue
        for name, getter in METRICS[app]:
            row: dict = {"app": app, "metric": name}
            vals = {arm: _values(app_records, arm, getter) for arm in ARMS}
            for arm in ARMS:
                v = vals[arm]
                row[f"n_{arm}"] = len(v)
                row[f"mean_{arm}"] = statistics.fmean(v) if v else None
                row[f"median_{arm}"] = statistics.median(v) if v else None
            boot = bootstrap_diff(
                vals[DEFAULT], vals[TUNED], n_resamples=n_resamples, seed=seed
            )
            row["diff"] = boot.diff if boot else None
            row["ci_lo"] = boot.lo if boot else None
            row["ci_hi"] = boot.hi if boot else None
            rows.append(row)
    return rows


def classifier_agreement(records: list[dict], arm: str) -> float | None:
    """Share of terms whose sensitive flag is identical across same-day runs.

    trend_scout runs of ``arm`` are grouped by UTC start date (the Google Trends
    list refreshes daily); within a day, every term seen by >= 2 runs counts
    once, agreeing when all its flags match. ``None`` without such a term.
    """
    by_day: dict[str, list[dict[str, bool]]] = defaultdict(list)
    for r in records:
        if r.get("arm") != arm or r.get("app") != "trend_scout":
            continue
        flags = _metrics(r).get("trend_flags")
        started = r.get("started_at")
        if not isinstance(flags, dict) or not flags:
            continue
        if not isinstance(started, int | float):
            continue
        day = datetime.datetime.fromtimestamp(started, tz=datetime.UTC).date()
        by_day[day.isoformat()].append({k: bool(v) for k, v in flags.items()})
    agreed = total = 0
    for runs in by_day.values():
        seen: dict[str, list[bool]] = defaultdict(list)
        for flags in runs:
            for term, sensitive in flags.items():
                seen[term].append(sensitive)
        for observations in seen.values():
            if len(observations) < 2:
                continue
            total += 1
            agreed += len(set(observations)) == 1
    return agreed / total if total else None


# --- the pre-registered rule -------------------------------------------------


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _combine(name: str, parts: list[tuple[str, str]]) -> CheckResult:
    statuses = [s for s, _ in parts]
    if "FAIL" in statuses:
        status = "FAIL"
    elif "N/A" in statuses or not statuses:
        status = "N/A"
    else:
        status = "PASS"
    return CheckResult(name, status, "; ".join(d for _, d in parts) or "no data")


def _mean_pair(
    records: list[dict], getter: Callable[[dict], float | None]
) -> tuple[float | None, float | None]:
    d, t = _values(records, DEFAULT, getter), _values(records, TUNED, getter)
    return (
        statistics.fmean(d) if d else None,
        statistics.fmean(t) if t else None,
    )


def _not_worse(label: str, default: float | None, tuned: float | None):
    """Sub-check ``default <= tuned`` on means (lower is better)."""
    detail = f"{label}: default {_fmt(default)} vs tuned {_fmt(tuned)}"
    if default is None or tuned is None:
        return "N/A", detail
    return ("PASS" if default <= tuned + EPS else "FAIL"), detail


def _diff_check(
    label: str,
    records: list[dict],
    getter: Callable[[dict], float | None],
    *,
    min_diff: float | None = None,
    max_diff: float | None = None,
):
    default, tuned = _mean_pair(records, getter)
    if default is None or tuned is None:
        return "N/A", f"{label}: no data"
    diff = default - tuned
    ok = True
    bound = ""
    if min_diff is not None:
        ok = ok and diff >= min_diff - EPS
        bound = f">= {min_diff:+.2f}"
    if max_diff is not None:
        ok = ok and diff <= max_diff + EPS
        bound = f"<= {max_diff:+.2f}"
    return ("PASS" if ok else "FAIL"), f"{label} diff {diff:+.3f} ({bound})"


def check_completion(records: list[dict]) -> CheckResult:
    failed = {
        arm: sum(1 for r in records if r.get("arm") == arm and not run_ok(r))
        for arm in ARMS
    }
    n = {arm: sum(1 for r in records if r.get("arm") == arm) for arm in ARMS}
    detail = (
        f"failed/unfinished default {failed[DEFAULT]}/{n[DEFAULT]} vs "
        f"tuned {failed[TUNED]}/{n[TUNED]}"
    )
    if not n[DEFAULT] or not n[TUNED]:
        return CheckResult("completion", "N/A", detail)
    ok = failed[DEFAULT] <= failed[TUNED] + MAX_EXTRA_FAILURES
    return CheckResult("completion", "PASS" if ok else "FAIL", detail)


def check_robustness(records: list[dict]) -> CheckResult:
    return _combine(
        "robustness",
        [
            _not_worse("abnormal+empty turns/run", *_mean_pair(records, _turns)),
            _not_worse(
                "ValidationErrors/run",
                *_mean_pair(records, _logkey("validation_errors")),
            ),
            _not_worse(
                "retry_exhausted/run",
                *_mean_pair(records, _metric("n_retry_exhausted")),
            ),
        ],
    )


def check_creative_quality(records: list[dict]) -> CheckResult:
    creative = _of_app(records, "creative_agent")
    return _combine(
        "creative_quality",
        [
            _diff_check(
                "gates_pass_rate",
                creative,
                _metric("gates_pass_rate"),
                min_diff=MIN_GATES_DIFF,
            ),
            _diff_check(
                "mean_score", creative, _metric("mean_score"), min_diff=MIN_SCORE_DIFF
            ),
            _diff_check(
                "n_issues", creative, _metric("n_issues"), max_diff=MAX_ISSUES_DIFF
            ),
        ],
    )


def check_creative_diversity(records: list[dict]) -> CheckResult:
    creative = _of_app(records, "creative_agent")
    return _combine(
        "creative_diversity",
        [
            _diff_check(
                "headline_jaccard",
                creative,
                _metric("headline_jaccard"),
                max_diff=MAX_JACCARD_DIFF,
            )
        ],
    )


def check_trend_scout_stability(records: list[dict]) -> CheckResult:
    default = classifier_agreement(records, DEFAULT)
    tuned = classifier_agreement(records, TUNED)
    detail = f"classifier agreement default {_fmt(default)} vs tuned {_fmt(tuned)}"
    if default is None or tuned is None:
        return CheckResult("trend_scout_stability", "N/A", detail)
    ok = default >= tuned - EPS
    return CheckResult("trend_scout_stability", "PASS" if ok else "FAIL", detail)


def check_latency(records: list[dict]) -> CheckResult:
    parts = []
    for app in APPS:
        ok_runs = [r for r in _of_app(records, app) if run_ok(r)]
        d, t = _values(ok_runs, DEFAULT, _wall), _values(ok_runs, TUNED, _wall)
        if not d or not t:
            continue
        md, mt = statistics.median(d), statistics.median(t)
        ok = md <= MAX_LATENCY_RATIO * mt + EPS
        parts.append(
            (
                "PASS" if ok else "FAIL",
                f"{app} median wall default {md:.0f}s vs tuned {mt:.0f}s "
                f"(limit {MAX_LATENCY_RATIO * mt:.0f}s)",
            )
        )
    return _combine("latency", parts)


def evaluate_rule(records: list[dict]) -> list[CheckResult]:
    return [
        check_completion(records),
        check_robustness(records),
        check_creative_quality(records),
        check_creative_diversity(records),
        check_trend_scout_stability(records),
        check_latency(records),
    ]


def verdict(checks: list[CheckResult]) -> str:
    statuses = {c.status for c in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "N/A" in statuses:
        return "INCONCLUSIVE"
    return "PASS"


# --- outputs -----------------------------------------------------------------

CSV_BASE = [
    "cell_id",
    "arm",
    "app",
    "campaign",
    "rep",
    "status",
    "ok",
    "session_id",
    "revision",
    "wall_s",
    "harness_wall_s",
    "error",
]
CSV_METRICS = [
    "finalized",
    "gates_pass_rate",
    "mean_score",
    "pass_rate",
    "n_issues",
    "n_retry_exhausted",
    "image_qa_attempts",
    "headline_jaccard",
    "completed",
    "n_selected",
    "info_gtrends_exhausted",
]


def write_csv(records: list[dict], path: Path) -> None:
    fields = CSV_BASE + CSV_METRICS + list(LOG_COUNT_KEYS)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, restval="")
        writer.writeheader()
        for r in records:
            row = {k: r.get(k) for k in CSV_BASE if k != "ok"}
            row["ok"] = run_ok(r)
            m = _metrics(r)
            row.update({k: m.get(k) for k in CSV_METRICS if k in m})
            counts = r.get("log_counts")
            if isinstance(counts, dict):
                row.update({k: counts.get(k) for k in LOG_COUNT_KEYS})
            writer.writerow(row)


def render_summary(rows: list[dict], checks: list[CheckResult]) -> str:
    lines = [
        "# Gemini 3 temperature A/B: summary",
        "",
        f"Overall verdict: **{verdict(checks)}**",
        "",
        "## Pre-registered rule",
        "",
        "| check | result | detail |",
        "|---|---|---|",
    ]
    lines += [f"| {c.name} | {c.status} | {c.detail} |" for c in checks]
    for app in APPS:
        app_rows = [r for r in rows if r["app"] == app]
        if not app_rows:
            continue
        lines += [
            "",
            f"## {app}",
            "",
            "| metric | n tuned | n default | mean tuned | mean default "
            "| median tuned | median default | default − tuned [95% CI] |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in app_rows:
            ci = (
                "n/a"
                if r["diff"] is None
                else f"{r['diff']:+.3f} [{r['ci_lo']:+.3f}, {r['ci_hi']:+.3f}]"
            )
            lines.append(
                f"| {r['metric']} | {r['n_tuned']} | {r['n_default']} "
                f"| {_fmt(r['mean_tuned'])} | {_fmt(r['mean_default'])} "
                f"| {_fmt(r['median_tuned'])} | {_fmt(r['median_default'])} | {ci} |"
            )
    return "\n".join(lines) + "\n"


def write_outputs(
    records: list[dict], out_dir: Path, *, n_resamples: int = N_RESAMPLES
) -> str:
    rows = metric_table(records, n_resamples=n_resamples)
    checks = evaluate_rule(records)
    summary = render_summary(rows, checks)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.md").write_text(summary)
    write_csv(records, out_dir / "records.csv")
    return summary


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results-root", type=Path, default=RESULTS_ROOT)
    p.add_argument("--resamples", type=int, default=N_RESAMPLES)
    args = p.parse_args(argv)
    records = load_records(args.results_root)
    if not records:
        raise SystemExit(f"no records under {args.results_root}")
    print(write_outputs(records, args.results_root, n_resamples=args.resamples))


if __name__ == "__main__":
    main()
