"""Deterministic post-render steps of creative_agent (``finalize_pipeline``).

The Pro root used to make five separate tool decisions after rendering (the
``creative_eval_agent`` AgentTool, then four persistence tools), and it
sometimes ended the run with an empty turn after one of the long results. These
graph function nodes run the same work as one deterministic unit:

* :func:`evaluate_creatives_node` — scores every creative with the
  ``creative_eval`` judge (``evaluate_all_creatives``) off the event loop;
* :func:`persist_node` — saves the eval report JSON + HTML gallery to GCS and
  writes the ``trend_creatives`` + ``creative_evals`` BigQuery rows;
* :func:`finalize_ready` — the truthy terminal: a compact summary for the root;
  it also sets ``finalize_done`` (runserver's auto-continue completion key).

Every step is isolated: a failure is logged and recorded as a generic
degradation marker (``<key>__retry_exhausted`` / ``<key>__issues``, surfaced by
``agent_common.collect_degradation_warnings``), never raised, so one failing
save cannot cost the run its other outputs or stall the root's tool call.
Transient infra errors (5xx/429/timeouts) are retried a bounded number of times
first (:func:`_run_step`).

The node functions take the ADK ``Context`` a graph function node receives. In
ADK 2.x ``ToolContext`` is an alias of ``Context``, so the existing tool
functions are called with it unchanged: their ``ctx.state`` writes land in the
node event's state delta.
"""

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

from google.adk.agents.context import Context

from agent_common import is_populated
from creative_eval import agent as eval_agent
from creative_eval.dimensions import dimension_labels_csv, gate_label

from . import bq_tools, gcs_tools, tools

logger = logging.getLogger(__name__)

REPORT_KEY = "creative_evaluation_report"
REPORT_EXHAUSTED_KEY = f"{REPORT_KEY}__retry_exhausted"

# State key each persistence step produces; a failed step records
# `<key>__issues` (a short message) instead.
EVAL_GCS_KEY = "eval_report_gcs_uri"
GALLERY_KEY = "creative_gallery_gcs_uri"
TRENDS_ROW_KEY = "creative_row_uuid"
EVAL_ROW_KEY = "eval_bq_row_uuid"
RESEARCH_PDF_KEY = "research_report_gcs_uri"
# Set by finalize_ready on every path: the creative apps' auto-continue
# completion key (runserver.async_runs._COMPLETION_KEYS). Not eval_bq_row_uuid,
# which is never written when there is no report or the eval BQ write failed.
DONE_KEY = "finalize_done"

# Backoff (seconds) before each retry of a transient persistence error: len + 1
# attempts in total. `_sleep` is the awaitable used (tests monkeypatch both).
_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0)
_sleep = asyncio.sleep

# The steps the summary reports as failed when their `__issues` marker is set,
# with a readable name. Research PDF is saved inside combined_research_pipeline
# but is listed here so the final summary names every failed save.
_STEP_LABELS = {
    RESEARCH_PDF_KEY: "research PDF",
    EVAL_GCS_KEY: "eval report (GCS)",
    GALLERY_KEY: "HTML gallery",
    TRENDS_ROW_KEY: "creative row (BigQuery)",
    EVAL_ROW_KEY: "eval row (BigQuery)",
}

_ISSUE_MAX_CHARS = 200
_MAX_FAILED_CREATIVES = 5


def issue_message(label: str, exc: BaseException) -> str:
    """A short, single-line ``<key>__issues`` message for a failed step."""
    detail = " ".join(str(exc).split()) or type(exc).__name__
    return f"{label} failed: {type(exc).__name__}: {detail}"[:_ISSUE_MAX_CHARS]


# --- evaluation --- #


async def evaluate_creatives_node(ctx: Context) -> None:
    """Score every final creative with the LLM judge (``evaluate_all_creatives``).

    The judge makes blocking concurrent HTTP calls (~65-72 s), so it runs in a
    worker thread. The thread never touches ``ctx.state`` (an ADK ``State`` is
    not thread-safe): it gets a plain-dict snapshot behind a minimal ``.state``
    holder, and the report it writes there is copied back on the loop.

    No creatives (the judge returns ``{"status": "error"}``) or a judge failure
    records ``creative_evaluation_report__retry_exhausted`` and continues; the
    persistence step then skips the eval-report writes.
    """
    holder = SimpleNamespace(state=ctx.state.to_dict())
    try:
        result = await asyncio.to_thread(eval_agent.evaluate_all_creatives, holder)
    except Exception:
        logger.exception("finalize: creative evaluation failed")
        _mark_evaluation_failed(ctx)
        return None
    report = holder.state.get(REPORT_KEY)
    if (isinstance(result, dict) and result.get("status") == "error") or not (
        is_populated(report)
    ):
        message = result.get("message") if isinstance(result, dict) else None
        logger.warning("finalize: no evaluation report: %s", message or result)
        _mark_evaluation_failed(ctx)
        return None
    ctx.state[REPORT_KEY] = report
    if ctx.state.get(REPORT_EXHAUSTED_KEY):
        ctx.state[REPORT_EXHAUSTED_KEY] = False  # a re-run succeeded
    return None


def _mark_evaluation_failed(ctx: Context) -> None:
    """Record the exhausted marker and drop any stale (earlier-run) report.

    The snapshot handed to the judge carries the previous report, if any, so a
    failed re-run would otherwise leave it for persist_node and the summary.
    """
    ctx.state[REPORT_EXHAUSTED_KEY] = True
    if is_populated(ctx.state.get(REPORT_KEY)):
        ctx.state[REPORT_KEY] = None


def _has_report(state: Mapping[str, Any]) -> bool:
    """A usable evaluation report: populated and not from a failed evaluation."""
    return is_populated(state.get(REPORT_KEY)) and not state.get(REPORT_EXHAUSTED_KEY)


# --- persistence --- #


def _transient_exceptions() -> tuple[type[BaseException], ...]:
    """Exception types worth retrying: 5xx/429/deadline + connection/timeouts.

    ``google.api_core`` (a dependency of the GCS/BigQuery clients) is imported
    lazily and guarded, so the builtins still apply without it.
    """
    excs: list[type[BaseException]] = [ConnectionError, TimeoutError]
    try:
        from google.api_core import exceptions as gexc
    except ImportError:  # pragma: no cover - present wherever the tools run
        return tuple(excs)
    excs += [
        gexc.ServiceUnavailable,
        gexc.InternalServerError,
        gexc.TooManyRequests,
        gexc.GatewayTimeout,
        gexc.DeadlineExceeded,
    ]
    return tuple(excs)


async def _run_step(
    ctx: Context, key: str, step: Callable[[Context], Any]
) -> dict[str, Any] | None:
    """Run one persistence tool; on exception or error dict record ``<key>__issues``.

    A transient error (:func:`_transient_exceptions`) is retried after each
    ``_RETRY_DELAYS`` backoff (3 attempts in total); the tools are idempotent
    (session-derived BigQuery keys + MERGE, overwriting GCS uploads). Other
    exceptions and ``{"status": "error"}`` results are not retried.

    Returns the tool's result dict on success, else ``None``. Never raises.
    """
    label = _STEP_LABELS[key]
    transient = _transient_exceptions()
    attempts = len(_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            result = step(ctx)
            if inspect.isawaitable(result):
                result = await result
            break
        except Exception as exc:
            if isinstance(exc, transient) and attempt < attempts - 1:
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "finalize: %s transient error (attempt %d/%d), retrying in "
                    "%.0fs: %s",
                    label,
                    attempt + 1,
                    attempts,
                    delay,
                    exc,
                )
                await _sleep(delay)
                continue
            logger.exception("finalize: %s failed", label)
            ctx.state[f"{key}__issues"] = issue_message(label, exc)
            return None
    if isinstance(result, dict) and result.get("status") == "error":
        message = str(result.get("message") or "returned an error")
        logger.warning("finalize: %s: %s", label, message)
        ctx.state[f"{key}__issues"] = f"{label} failed: {message}"[:_ISSUE_MAX_CHARS]
        return None
    return result if isinstance(result, dict) else {}


def _skip(ctx: Context, key: str, reason: str) -> None:
    logger.warning("finalize: skipped %s: %s", _STEP_LABELS[key], reason)
    ctx.state[f"{key}__issues"] = f"{_STEP_LABELS[key]} skipped: {reason}"


async def persist_node(ctx: Context) -> None:
    """Save the eval report + gallery to GCS and write both BigQuery rows.

    Fixed order, one step at a time: eval report JSON (GCS) → HTML gallery
    (GCS) → creative row (BigQuery, sets ``creative_row_uuid``) → eval row
    (BigQuery), last because it links to both ``creative_row_uuid`` and
    ``eval_report_gcs_uri``. The sync tools run inline on the event loop (as
    they did as root tools: ADK runs sync tools inline unless a tool thread pool
    is configured) because they write ``ctx.state``, which must not be touched
    from a worker thread; the gallery's own blocking I/O already runs in
    threads. Running the three independent saves concurrently would save a few
    seconds at most; a deterministic order keeps state writes simple.

    Without an evaluation report both eval writes are skipped (the GCS tool
    would only return an error) and only the gallery + creative row are saved.
    """
    has_report = _has_report(ctx.state.to_dict())
    if has_report:
        await _run_step(ctx, EVAL_GCS_KEY, gcs_tools.save_eval_report_to_gcs)

    if is_populated(ctx.state.get("final_visual_concepts")) and is_populated(
        ctx.state.get("ad_copy_critique")
    ):
        gallery = await _run_step(ctx, GALLERY_KEY, tools.save_creative_gallery_html)
        if gallery and gallery.get("gcs_uri"):
            ctx.state[GALLERY_KEY] = gallery["gcs_uri"]
    else:
        _skip(ctx, GALLERY_KEY, "no final ad copies or visual concepts")

    trends = await _run_step(ctx, TRENDS_ROW_KEY, bq_tools.write_trends_to_bq)
    if trends is None and ctx.state.get(TRENDS_ROW_KEY):
        # write_trends_to_bq sets creative_row_uuid BEFORE its MERGE: clear it so
        # the eval row does not link to a creative row that was never written.
        ctx.state[TRENDS_ROW_KEY] = ""

    if has_report:
        # Written even when an earlier save failed: the eval row keeps the
        # scores, with an empty link column for the missing creative row
        # (cleared above) / eval report URI (never set when that save failed).
        await _run_step(ctx, EVAL_ROW_KEY, bq_tools.write_eval_report_to_bq)
    return None


# --- terminal summary --- #


def _percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "n/a"


def _score(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _failed_gate_labels(score: Mapping[str, Any]) -> list[str]:
    """Labels of the failed non-advisory gates of one creative's score."""
    return [
        gate_label(str(g.get("gate") or ""))
        for g in score.get("gates") or []
        if isinstance(g, Mapping)
        and not g.get("passed", True)
        and not g.get("advisory")
    ]


def _judge_failed(score: Mapping[str, Any]) -> bool:
    """True for the zero-score placeholder of a failed judge call."""
    return "evaluation_failed" in (score.get("improvements") or [])


def _gates_line(report: Mapping[str, Any], rate: Any) -> str:
    """``N/M passed all checks (gates pass rate X%).`` over judged creatives.

    "" when the report has no numeric ``gates_pass_rate`` (pre-gate reports,
    or no creative judged). M excludes failed judge calls, matching the rate.
    """
    if isinstance(rate, bool) or not isinstance(rate, int | float):
        return ""
    scores = [
        item.get("score") or {}
        for key in ("ad_copy_evaluations", "visual_concept_evaluations")
        for item in report.get(key) or []
        if isinstance(item, Mapping)
    ]
    judged = [s for s in scores if isinstance(s, Mapping) and not _judge_failed(s)]
    ok = sum(1 for s in judged if s.get("gates_passed", True))
    return f"{ok}/{len(judged)} passed all checks (gates pass rate {_percent(rate)})."


def _failed_creatives(report: Mapping[str, Any]) -> list[str]:
    """``'name' (kind, score[, failed checks: …])`` per creative that did not pass.

    A creative fails on a below-threshold score or a failed gate.
    """
    failed: list[str] = []
    for kind, list_key, name_key in (
        ("ad copy", "ad_copy_evaluations", "headline"),
        ("visual", "visual_concept_evaluations", "concept_name"),
    ):
        for item in report.get(list_key) or []:
            if not isinstance(item, Mapping):
                continue
            score = item.get("score") or {}
            if not score.get("passed", True):
                name = " ".join(str(item.get(name_key) or "untitled").split())[:60]
                gates = _failed_gate_labels(score)
                if _judge_failed(score):
                    checks = ", evaluation failed"
                elif gates:
                    checks = f", failed checks: {', '.join(gates)}"
                else:
                    checks = ""
                failed.append(
                    f"'{name}' ({kind}, {_score(score.get('overall_score'))}{checks})"
                )
    return failed


def _failed_steps(state: Mapping[str, Any]) -> list[str]:
    steps = []
    if state.get(REPORT_EXHAUSTED_KEY):
        steps.append("evaluation")
    for key, label in _STEP_LABELS.items():
        if state.get(f"{key}__issues"):
            steps.append(label)
    return steps


def finalize_summary(state: Mapping[str, Any]) -> str:
    """The root's compact ``finalize_pipeline`` result for a state snapshot (pure).

    Always non-empty: with no evaluation report it says so (naming the key) and
    still lists what was saved.
    """
    parts: list[str] = []
    report = state.get(REPORT_KEY)
    if _has_report(state) and isinstance(report, Mapping):
        summary = report.get("summary") or {}
        ad_total = int(summary.get("total_ad_copies") or 0)
        vis_total = int(summary.get("total_visual_concepts") or 0)
        passed = int(summary.get("ad_copies_passed") or 0) + int(
            summary.get("visual_concepts_passed") or 0
        )
        parts.append(
            f"Evaluation complete: {passed}/{ad_total + vis_total} creatives "
            f"passed (pass rate {_percent(summary.get('overall_pass_rate'))}); "
            f"{ad_total} ad copies (avg score "
            f"{_score(summary.get('avg_ad_copy_score'))}), {vis_total} visual "
            f"concepts (avg score {_score(summary.get('avg_visual_score'))})."
        )
        gates_line = _gates_line(report, summary.get("gates_pass_rate"))
        if gates_line:
            parts.append(gates_line)
        weakest = summary.get("weakest_dimensions") or []
        if weakest:
            parts.append(f"Weakest dimensions: {dimension_labels_csv(weakest)}.")
        failed = _failed_creatives(report)
        if failed:
            shown = ", ".join(failed[:_MAX_FAILED_CREATIVES])
            more = len(failed) - _MAX_FAILED_CREATIVES
            parts.append(
                f"Did not pass: {shown}" + (f" (+{more} more)." if more > 0 else ".")
            )
    else:
        parts.append(
            f"creative evaluation did not produce '{REPORT_KEY}' (no creatives to "
            "evaluate, or the judge failed); no scores are available for this run."
        )

    saved = [
        f"{label} {state[key]}"
        for key, label in (
            (EVAL_GCS_KEY, "eval report"),
            (GALLERY_KEY, "HTML gallery"),
            (RESEARCH_PDF_KEY, "research PDF"),
        )
        if is_populated(state.get(key))
    ]
    if saved:
        parts.append("Saved: " + "; ".join(saved) + ".")
    failed_steps = _failed_steps(state)
    if failed_steps:
        parts.append("Failed steps: " + ", ".join(failed_steps) + ".")
    parts.append("Write the final summary for the user now.")
    return " ".join(parts)


def finalize_ready(ctx: Context) -> str:
    """Terminal node of finalize_pipeline (the root's tool result; always truthy).

    Sets ``finalize_done`` on every path, so runserver's auto-continue never
    re-runs finalize (and its ~70 s judge) once it has finished.
    """
    ctx.state[DONE_KEY] = True
    return finalize_summary(ctx.state.to_dict())
