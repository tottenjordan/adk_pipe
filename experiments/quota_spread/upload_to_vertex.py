"""Post-hoc mirror of quota-spread run records into Agent Platform Experiments.

**Downstream sink, never the hot path.** ``aiplatform.log_*`` are network
round-trips to ``aiplatform.googleapis.com``; calling them inside the timed
``run_batch`` closure would inject jitter into the very ``research_s``/``total_s``
being measured (see DoE §12). So this walks the *already-written* ``results/``
tree via :func:`analyze.load_records` **after** the batches finish and creates one
``ExperimentRun`` per record. The committed JSON stays the source of truth and
``analyze.py`` stays stdlib-only + authoritative for the H1 slope; this is a
dashboard/store bolt-on for sortable side-by-side comparison in the console.

The record→(run_name, params, metrics) and record→time-series shaping are both
pure and unit-tested; the ``aiplatform`` calls in :func:`upload` (scalar metrics
plus per-run phase-progression time series) are the live path (integration only).

Usage (after a live DoE batch, in a cool window — no model quota is touched):
    PYTHONPATH="$PWD" GOOGLE_CLOUD_PROJECT=<proj> \\
      uv run python -m experiments.quota_spread.upload_to_vertex \\
      [--experiment quota-bucket-spread-doe] [--location us-central1] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from .analyze import load_records
from .quality import extract_quality
from .run_batch import RESULTS_ROOT

EXPERIMENT_NAME = "quota-bucket-spread-doe"

# Vertex experiment-run names: lowercase alphanumerics + hyphens only, <=128.
_SANITIZE = re.compile(r"[^a-z0-9-]+")

# Canonical sequential order of the creative_agent pipeline phases (matches the
# AgentTool spans in parse_run._SPAN_TOOLS). The per-run time series steps by
# this order so the SAME phase lands on the SAME x-position across every run —
# the console overlays all runs' curves aligned by phase, and the N=5 contention
# tail shows up as diverging ``cumulative_wall_s`` lines. The cross-cutting
# ``orchestrator``/``runserver`` buckets are overhead, not pipeline stages, so
# they stay out of the linear progression curve.
_PHASE_SERIES_ORDER: tuple[str, ...] = (
    "research",
    "ad_copy",
    "visual",
    "eval",
    "persistence",
)


def _run_name(record: dict) -> str:
    """Deterministic, Vertex-valid run name: ``<arm>-n<N>-<batch>-<sid8>``.

    Underscores (``global_3x``, ``regional_25``, batch ids) become hyphens; the
    session-id prefix (or a batch fallback for error records with no session)
    keeps names unique within a cell.
    """
    arm = str(record.get("arm") or "arm")
    n = record.get("concurrency")
    batch = str(record.get("batch_id") or "b")
    sid = record.get("session_id") or "nosession"
    tail = str(sid)[:8]
    raw = f"{arm}-n{n}-{batch}-{tail}".lower()
    return _SANITIZE.sub("-", raw)[:128].strip("-")


def record_to_run(record: dict) -> tuple[str, dict, dict]:
    """Shape one run record into ``(run_name, params, metrics)`` — pure.

    Params are the design factors (categorical/identifiers); metrics are the
    measured responses (numeric), harvested including the free ``creative_eval``
    quality (pass-rate + grand-mean score) via :func:`quality.extract_quality`.
    ``None`` metrics are dropped so a partial/error run logs what it has.
    """
    params = {
        "arm": record.get("arm"),
        "concurrency": record.get("concurrency"),
        "revision": record.get("revision"),
        "batch_id": record.get("batch_id"),
        "status": record.get("status"),
    }
    params = {k: v for k, v in params.items() if v is not None}

    metrics: dict[str, float] = {}
    for key in ("research_s", "visual_s", "eval_s", "total_s", "count_429"):
        val = record.get(key)
        if val is not None:
            metrics[key] = float(val)

    quality = extract_quality(record.get("state") or {})
    if quality:
        if quality.get("pass_rate") is not None:
            metrics["eval_pass"] = float(quality["pass_rate"])
        if quality.get("mean_score") is not None:
            metrics["eval_mean"] = float(quality["mean_score"])

    return _run_name(record), params, metrics


def record_to_timeseries(record: dict) -> list[tuple[int, dict[str, float]]]:
    """Shape one record into ordered ``(step, metrics)`` time-series points — pure.

    Emits one point per pipeline phase present in the run's ``summary.phase_wall_s``,
    stepped by that phase's CANONICAL index in :data:`_PHASE_SERIES_ORDER` (so a
    run missing a phase keeps every other phase on its own x-position, and runs
    stay aligned for the console overlay). Each point carries that phase's
    ``phase_duration_s`` and the run's ``cumulative_wall_s`` through it. Returns
    ``[]`` when no phase timing is available (e.g. an error record with no
    summary), so the caller logs nothing for it.
    """
    summary = record.get("summary") or {}
    phase_wall = summary.get("phase_wall_s") or {}
    points: list[tuple[int, dict[str, float]]] = []
    cumulative = 0.0
    for step, phase in enumerate(_PHASE_SERIES_ORDER):
        dur = phase_wall.get(phase)
        if dur is None:
            continue
        cumulative += float(dur)
        points.append(
            (step, {"phase_duration_s": float(dur), "cumulative_wall_s": cumulative})
        )
    return points


def upload(
    *,
    results_root: Path | str = RESULTS_ROOT,
    experiment: str = EXPERIMENT_NAME,
    project: str | None = None,
    location: str = "us-central1",
    dry_run: bool = False,
) -> int:
    """Create one ExperimentRun per record. Returns the count uploaded/planned.

    ``dry_run`` prints the shaped runs without importing/calling ``aiplatform``,
    so the shaping can be eyeballed offline before spending a GCP resource.
    """
    records = load_records(results_root)
    shaped = [(*record_to_run(r), record_to_timeseries(r)) for r in records]

    if dry_run:
        for name, params, metrics, series in shaped:
            print(
                f"{name}\n    params={params}\n    metrics={metrics}\n"
                f"    timeseries={len(series)} pt(s)",
                flush=True,
            )
        print(f"[dry-run] {len(shaped)} run(s) would be logged to '{experiment}'.")
        return len(shaped)

    import google.cloud.aiplatform as aiplatform  # noqa: PLC0415 — live-path only

    project = project or os.environ["GOOGLE_CLOUD_PROJECT"]
    aiplatform.init(experiment=experiment, project=project, location=location)
    print(f"[upload] {len(shaped)} run(s) -> experiment '{experiment}' @ {location}")
    logged = 0
    for name, params, metrics, series in shaped:
        # Idempotent: create a fresh run; if it already exists (re-upload), resume
        # it. ``resume=True`` alone 404s on the first upload (nothing to resume).
        try:
            run_ctx = aiplatform.start_run(name)
        except Exception:  # noqa: BLE001 — already-exists surfaces as varied types
            run_ctx = aiplatform.start_run(name, resume=True)
        with run_ctx:
            if params:
                aiplatform.log_params(params)
            if metrics:
                aiplatform.log_metrics(metrics)
            # Per-run phase-progression curve -> the experiment's backing
            # TensorBoard, rendered as line charts in the console's Time Series tab.
            for step, ts_metrics in series:
                aiplatform.log_time_series_metrics(ts_metrics, step=step)
        logged += 1
        print(
            f"  logged {name}: metrics={list(metrics)} timeseries={len(series)}pt",
            flush=True,
        )
    print(
        f"[upload] done ({logged}/{len(shaped)}). "
        f"View at console → Vertex AI → Experiments → {experiment}"
    )
    return len(shaped)


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results-root", default=str(RESULTS_ROOT))
    p.add_argument("--experiment", default=EXPERIMENT_NAME)
    p.add_argument("--project", default=None, help="defaults to $GOOGLE_CLOUD_PROJECT")
    p.add_argument("--location", default="us-central1")
    p.add_argument(
        "--dry-run", action="store_true", help="print shaped runs, no GCP calls"
    )
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    upload(
        results_root=args.results_root,
        experiment=args.experiment,
        project=args.project,
        location=args.location,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
