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
turning `*__retry_exhausted` markers (left by `RetryUntilKeyAgent`) into
human-readable degradation notes consumed by the eval report, BigQuery row, and
HTML gallery.

This module imports `google.adk`/`google.genai` but builds no genai client, so
it stays non-creds-gated and unit-testable offline.
"""

import logging
import warnings
from typing import Any

from google.genai import types
from google.adk.sessions.state import State
from google.adk.models.llm_response import LlmResponse
from google.adk.agents.callback_context import CallbackContext


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


_EXHAUSTED_SUFFIX = "__retry_exhausted"


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
    markers left by `RetryUntilKeyAgent`, making it trivial to see *where* a run
    stalled: e.g. `raw_gtrends` present but `info_gtrends` MISSING means the
    understand step was skipped or emitted an empty turn — the exact ambiguity
    that was impossible to resolve from logs alone during the 2026-07-14
    incident.
    """

    def log_final_state_summary(callback_context: CallbackContext) -> None:
        snapshot = _snapshot(callback_context.state)
        summary = {k: _describe_state_value(snapshot.get(k)) for k in keys}
        exhausted = sorted(k for k in snapshot if k.endswith(_EXHAUSTED_SUFFIX))
        logging.info(
            "%s final state [invocation=%s]: %s%s",
            agent_label,
            callback_context.invocation_id,
            summary,
            f" retry_exhausted={exhausted}" if exhausted else "",
        )

    return log_final_state_summary


def collect_degradation_warnings(state: State | dict[str, Any]) -> list[str]:
    """Turn `*__retry_exhausted` markers in state into human-readable notes.

    Single source of truth for degradation surfacing: the eval report, the
    `creative_evals` BigQuery row, and the HTML gallery all derive their notes
    from this. Returns a sorted list (one note per truthy marker), or `[]` when
    research completed cleanly.
    """
    snapshot = _snapshot(state)
    notes = []
    for key, value in snapshot.items():
        if key.endswith(_EXHAUSTED_SUFFIX) and value:
            step = key[: -len(_EXHAUSTED_SUFFIX)]
            notes.append(f"Step '{step}' exhausted retries and produced no output.")
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
