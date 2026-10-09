"""Shared debugging-observability callbacks for the agent packages.

Extracted from `trend_scout/callbacks.py` (WS3) so `creative_agent`,
`interactive_creative`, `creative_eval`, and `trend_scout` all share one
implementation of the three signals that made the 2026-07-14 incident
diagnosable:

- `log_run_start` — a run-start correlation line (`before_agent_callback`) tying
  a run to its session transcript. The frontend mints a throwaway user_id per
  submission, so without this a UI failure can't be traced to a session.
- `log_empty_turn_finish_reason` — an `after_model_callback` that warns only when
  a model turn produced no usable output (the producer-empty landmine root
  cause), staying quiet on the happy path.
- `make_final_state_summary(label, keys)` — a factory returning an
  `after_agent_callback` that logs the presence of an agent's load-bearing state
  keys plus any `*__retry_exhausted` markers, so it's trivial to see *where* a
  run stalled.

Plus `collect_degradation_warnings(state)`, the single source of truth for
turning `<key>__retry_exhausted` markers (left by `RetryUntilKeyNode`) and
`<key>__issues` markers (residual quality issues a step recorded) into
human-readable degradation notes consumed by the eval report, BigQuery row, and
HTML gallery. Both conventions are generic; the only key names here are a small
display-label map for the issue notes (`_ISSUE_LABELS`), which carries no logic.

This module imports `google.adk`/`google.genai` but builds no genai client, so
it stays non-creds-gated and unit-testable offline.
"""

import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions.state import State
from google.genai import types

# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


_EXHAUSTED_SUFFIX = "__retry_exhausted"

# `<key>__issues`: a step's unresolved quality issues (a list of strings or one
# string), e.g. creative_agent's brief check after its bounded revision loop.
_ISSUES_SUFFIX = "__issues"
_ISSUE_EXAMPLE_CHARS = 80
_ISSUES_NOTE_MAX_CHARS = 200

# Display labels for the `<key>__issues` notes, keyed by state-key NAME only (no
# behaviour hangs off them). The notes are read by people (results page, HTML
# gallery "Run notes", BigQuery), and "Image qa" / "Eval bq row uuid" do not read
# naturally. Kept here rather than passed in by callers because the notes are
# built at three call sites in three packages (creative_agent's gallery,
# creative_eval's report, trend_scout's BQ row) that must render the SAME note,
# and creative_eval cannot import creative_agent (the dependency runs the other
# way). Persistence failures end in " save" — the frontend groups notes on that
# suffix (`frontend/src/lib/run-warnings.ts`). Unknown keys fall back to the
# sentence-cased key.
_ISSUE_LABELS: dict[str, str] = {
    "image_qa": "Image check",
    "person_reference": "Person reference",
    "ad_copy_critique": "Ad copy check",
    "final_visual_concepts": "Visual concept check",
    "creative_brief": "Creative brief",
    "research_report_gcs_uri": "Research PDF save",
    "eval_report_gcs_uri": "Eval report save",
    "creative_gallery_gcs_uri": "Gallery save",
    "creative_row_uuid": "Trend row save",
    "eval_bq_row_uuid": "Eval row save",
}


def log_run_start(callback_context: CallbackContext) -> None:
    """Log a run-start correlation line tying this run to a session transcript.

    Call at the top of an agent's `before_agent_callback`. The frontend mints a
    throwaway user_id per submission, so without this a UI failure can't be
    traced back to a specific session for post-mortem inspection.
    """
    logging.info(
        "run start: agent=%s invocation=%s session=%s user=%s",
        callback_context.agent_name,
        callback_context.invocation_id,
        callback_context.session.id,
        callback_context.user_id,
    )


def _describe_state_value(value: Any) -> str:
    """Compact, non-verbose description of a state value's presence."""
    if value is None:
        return "MISSING"
    if isinstance(value, str):
        return f"present(len={len(value)})" if value.strip() else "empty"
    if isinstance(value, (list, dict)):
        return f"present({type(value).__name__}, n={len(value)})"
    return "present"


def _snapshot(state: State | dict[str, Any]) -> dict[str, Any]:
    """Return a plain-dict view of session state.

    An ADK `State` supports `.get()`/`__contains__` but NOT iteration —
    `for k in state` falls back to integer indexing and raises `KeyError: 0`
    (the bug fixed in commit 9ec1c92). Always snapshot before scanning keys.
    """
    return state.to_dict() if isinstance(state, State) else dict(state)


def make_final_state_summary(agent_label: str, keys: tuple[str, ...]):
    """Build an `after_agent_callback` that logs an end-of-run state summary.

    `keys` are the agent's load-bearing state keys. The returned callback logs
    each key's presence (present/empty/MISSING) plus any `*__retry_exhausted`
    markers left by `RetryUntilKeyNode`, making it trivial to see *where* a run
    stalled: e.g. `raw_gtrends` present but `info_gtrends` MISSING means the
    understand step was skipped or emitted an empty turn — the exact ambiguity
    that was impossible to resolve from logs alone during the 2026-07-14
    incident.
    """

    def log_final_state_summary(callback_context: CallbackContext) -> None:
        snapshot = _snapshot(callback_context.state)
        summary = {k: _describe_state_value(snapshot.get(k)) for k in keys}
        exhausted = sorted(
            k for k, v in snapshot.items() if k.endswith(_EXHAUSTED_SUFFIX) and v
        )
        logging.info(
            "%s final state [invocation=%s]: %s%s",
            agent_label,
            callback_context.invocation_id,
            summary,
            f" retry_exhausted={exhausted}" if exhausted else "",
        )

    return log_final_state_summary


def _label(key: str) -> str:
    """A state key's display label: a known name, else sentence case
    ("some_step" -> "Some step")."""
    if key in _ISSUE_LABELS:
        return _ISSUE_LABELS[key]
    return key.replace("_", " ").strip().capitalize() or key


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _issues_note(key: str, value: Any) -> str | None:
    items = value if isinstance(value, list) else [value]
    items = [str(i).strip() for i in items if i is not None and str(i).strip()]
    if not items:
        return None
    note = (
        f"{_label(key)} has unresolved issues: {len(items)} "
        f"(e.g. {_truncate(items[0], _ISSUE_EXAMPLE_CHARS)})"
    )
    return _truncate(note, _ISSUES_NOTE_MAX_CHARS)


def collect_degradation_warnings(state: State | dict[str, Any]) -> list[str]:
    """Turn degradation markers in state into human-readable notes.

    Two generic conventions:
    - `<key>__retry_exhausted` (truthy) — the step producing `<key>` gave up
      (`RetryUntilKeyNode`, or a `FailSoftNode` that converted an exception).
      A falsy value (`None`: cleared by a later successful run) is ignored.
    - `<key>__issues` (non-empty list of strings, or a string; `None`/blank
      items are dropped) — the step's
      output exists but has unresolved quality issues; the note gives the count
      and the first issue, truncated (total note length capped).

    Single source of truth for degradation surfacing: the eval report, the
    `creative_evals` BigQuery row, and the HTML gallery all derive their notes
    from this. Returns a sorted list (one note per truthy marker), or `[]` when
    the run completed cleanly.
    """
    snapshot = _snapshot(state)
    notes = []
    for key, value in snapshot.items():
        if not value:
            continue
        if key.endswith(_EXHAUSTED_SUFFIX):
            step = key[: -len(_EXHAUSTED_SUFFIX)]
            notes.append(f"Step '{step}' exhausted retries and produced no output.")
        elif key.endswith(_ISSUES_SUFFIX) and isinstance(value, list | str):
            note = _issues_note(key[: -len(_ISSUES_SUFFIX)], value)
            if note:
                notes.append(note)
    return sorted(notes)


def log_empty_turn_finish_reason(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> None:
    """Log finish_reason + token usage when a model turn produced no usable output.

    Set as `after_model_callback` on the thinking/tool agents. This is the
    root-cause signal for the producer-empty landmine: a thinking agent that
    burns its output budget returns finish_reason=MAX_TOKENS (or
    MALFORMED_FUNCTION_CALL) with no text and no tool call, silently leaving its
    `output_key` unset. Normal text turns (STOP + text) and tool-call turns
    (STOP + function_call) are NOT logged, so this stays quiet on the happy path.
    """
    if llm_response is None or llm_response.partial:
        return None

    parts = (
        llm_response.content.parts
        if llm_response.content and llm_response.content.parts
        else []
    )
    has_text = any(getattr(p, "text", None) for p in parts)
    has_func = any(getattr(p, "function_call", None) for p in parts)
    finish_reason = llm_response.finish_reason

    is_normal = finish_reason in (
        None,
        types.FinishReason.STOP,
    ) and (has_text or has_func)
    if is_normal:
        return None

    usage = llm_response.usage_metadata
    logging.warning(
        "empty/abnormal model turn in %s [invocation=%s]: finish_reason=%s "
        "has_text=%s has_func_call=%s prompt_tokens=%s candidates_tokens=%s "
        "thoughts_tokens=%s",
        callback_context.agent_name,
        callback_context.invocation_id,
        finish_reason,
        has_text,
        has_func,
        getattr(usage, "prompt_token_count", None) if usage else None,
        getattr(usage, "candidates_token_count", None) if usage else None,
        getattr(usage, "thoughts_token_count", None) if usage else None,
    )
    return None
