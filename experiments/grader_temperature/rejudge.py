"""Re-judge each selected session's creatives at several judge temperatures.

Judge inputs are built from the cached state exactly like a full evaluation in
``creative_eval/agent.py`` (``evaluate_all_creatives``: same helpers, same
keyword arguments, all ad copies judged, no early-partial reuse). Each
(session, temperature, repeat) is ONE ``evaluate_all_concurrently`` batch with
its own client and 2 workers, written to ``results/judge/<sid>/<t>-r<k>.json``.
Arms are interleaved per session (``common.plan_order``); existing results are
skipped, so an interrupted run resumes. A creative whose judge call failed
(``"evaluation_failed"`` in its improvements) is listed under ``failed`` and
excluded by ``analyze``. A batch that raises writes nothing (retried on resume).

Quota: the judge is the Pro model (5 RPM, project-wide, shared with live runs);
~8 calls per batch, so 10 sessions × 2 temperatures × 3 repeats ≈ 2 h.

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.grader_temperature.rejudge \\
      --temps 0.3,1.0 --repeats 3
"""

from __future__ import annotations

import argparse
import datetime
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .common import (
    EVALUATION_FAILED,
    JUDGE_DIR,
    SELECTION_PATH,
    STATE_DIR,
    load_selection,
    load_state,
    parse_temps,
    plan_order,
    run_filename,
    write_json,
)

logger = logging.getLogger(__name__)
WORKERS = 2


def judge_inputs(state: Mapping[str, Any]) -> dict[str, Any]:
    """``evaluate_all_concurrently`` keyword arguments for a full evaluation.

    The same helpers and arguments as ``creative_eval.agent.evaluate_all_creatives``
    with nothing reused from an early ad-copy partial (every copy is judged).
    """
    from creative_eval.agent import _campaign_context, _state_items
    from creative_eval.brief import parse_brief
    from creative_eval.evaluate import safer_angle_active, safer_angle_reason

    ad_copies = _state_items(state, "ad_copy_critique", "ad_copies")
    return {
        "ad_copies": ad_copies,
        "visual_concepts": _state_items(
            state, "final_visual_concepts", "visual_concepts"
        ),
        "campaign_context": _campaign_context(state),
        "brief": parse_brief(state.get("creative_brief")),
        "generated_images": state.get("generated_images") or {},
        "person_reference": state.get("person_reference"),
        "trend_situation": state.get("trend_situation"),
        "sources": state.get("sources"),
        "paired_ad_copies": ad_copies,
        "safer_angle": safer_angle_active(state),
        "sensitive_reason": safer_angle_reason(state),
    }


def make_eval_config(temperature: float) -> Any:
    """The judge config for one arm: ``temperature`` and 2 workers per batch."""
    from creative_eval.config import EvalConfig

    return EvalConfig(
        judge_temperature=temperature,
        max_eval_workers=WORKERS,
        max_visual_wave_workers=WORKERS,
    )


def _default_evaluate() -> Callable[..., Any]:
    from creative_eval.evaluate import evaluate_all_concurrently

    return evaluate_all_concurrently


def _default_client_factory() -> Callable[[Any], Any]:
    from creative_eval.evaluate import _get_client

    return _get_client


def failed_creatives(ad_evals: Sequence[Any], visual_evals: Sequence[Any]) -> list:
    """The creatives whose judge call failed (zero-score placeholder)."""
    return [
        {"kind": "ad_copy", "original_id": e.original_id, "headline": e.headline}
        for e in ad_evals
        if EVALUATION_FAILED in e.score.improvements
    ] + [
        {"kind": "visual", "concept_name": e.concept_name}
        for e in visual_evals
        if EVALUATION_FAILED in e.score.improvements
    ]


def judge_once(
    state: Mapping[str, Any],
    temperature: float,
    *,
    evaluate: Callable[..., Any],
    client_factory: Callable[[Any], Any],
    config_factory: Callable[[float], Any],
) -> dict[str, Any]:
    """One judged batch at ``temperature`` → the result record (no session ids)."""
    config = config_factory(temperature)
    inputs = judge_inputs(state)
    started = time.monotonic()
    with client_factory(config) as client:
        ad_evals, visual_evals = evaluate(
            inputs.pop("ad_copies"),
            inputs.pop("visual_concepts"),
            inputs.pop("campaign_context"),
            config,
            client=client,
            **inputs,
        )
    from creative_eval.dimensions import JUDGE_VERSION

    return {
        "temperature": temperature,
        "judge_model": getattr(config, "eval_model", ""),
        "judge_version": JUDGE_VERSION,
        "finished_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "elapsed_s": round(time.monotonic() - started, 2),
        "ad_copy_evaluations": [e.model_dump() for e in ad_evals],
        "visual_concept_evaluations": [e.model_dump() for e in visual_evals],
        "failed": failed_creatives(ad_evals, visual_evals),
    }


def run(
    session_ids: Sequence[str],
    temps: Sequence[float],
    repeats: int,
    *,
    state_dir: Path = STATE_DIR,
    out_dir: Path = JUDGE_DIR,
    evaluate: Callable[..., Any] | None = None,
    client_factory: Callable[[Any], Any] | None = None,
    config_factory: Callable[[float], Any] = make_eval_config,
) -> dict[str, list]:
    """Judge every planned (session, temperature, repeat) not yet on disk.

    Returns ``{written, skipped, errors, no_state}`` (``errors``: batches that
    raised; nothing was written for them).
    """
    evaluate = evaluate or _default_evaluate()
    client_factory = client_factory or _default_client_factory()
    report: dict[str, list] = {
        "written": [],
        "skipped": [],
        "errors": [],
        "no_state": [],
    }
    states: dict[str, dict | None] = {}
    for sid, t, k in plan_order(session_ids, temps, repeats):
        path = out_dir / sid / run_filename(t, k)
        if path.exists():
            report["skipped"].append(str(path))
            continue
        if sid not in states:
            states[sid] = load_state(sid, state_dir)
        state = states[sid]
        if state is None:
            if sid not in report["no_state"]:
                logger.warning("No cached state for %s (run fetch first)", sid)
                report["no_state"].append(sid)
            continue
        try:
            record = judge_once(
                state,
                t,
                evaluate=evaluate,
                client_factory=client_factory,
                config_factory=config_factory,
            )
        except Exception as exc:
            logger.error("Judge batch %s t=%s r%d failed: %s", sid, t, k, exc)
            report["errors"].append({"session_id": sid, "temperature": t, "repeat": k})
            continue
        record.update({"session_id": sid, "repeat": k})
        write_json(path, record)
        report["written"].append(str(path))
        logger.info(
            "Judged %s t=%s r%d (%d failed calls)", sid, t, k, len(record["failed"])
        )
    return report


def main(argv: Sequence[str] | None = None) -> dict[str, list]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--temps", default="0.3,1.0")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--selection", type=Path, default=SELECTION_PATH)
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--out-dir", type=Path, default=JUDGE_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    selection = load_selection(args.selection)
    report = run(
        selection["session_ids"],
        parse_temps(args.temps),
        args.repeats,
        state_dir=args.state_dir,
        out_dir=args.out_dir,
    )
    print(
        f"written {len(report['written'])}, skipped {len(report['skipped'])}, "
        f"errors {len(report['errors'])}, no state {len(report['no_state'])}"
    )
    return report


if __name__ == "__main__":
    main()
