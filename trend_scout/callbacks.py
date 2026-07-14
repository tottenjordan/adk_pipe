import time
import uuid
import logging
import warnings
import pandas as pd
from typing import Dict, Any

from google.genai import types
from google.adk.sessions.state import State
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.agents.callback_context import CallbackContext

from .config import config


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


def _set_initial_states(source: Dict[str, Any], target: State | dict[str, Any]):
    """
    Setting the initial session state given a JSON object of states.

    Args:
        source: A JSON object of states.
        target: The session state object to insert into.
    """
    unique_id = f"{str(uuid.uuid4())[:4]}"
    formatted_now = pd.Timestamp.now("UTC").strftime("%Y_%m_%d_%H_%M")
    if config.state_init not in target:
        target[config.state_init] = True
        target["gcs_bucket"] = config.GCS_BUCKET
        target["agent_output_dir"] = "trawler_output"
        target["gcs_folder"] = f"{formatted_now}_{unique_id}"
        logging.info(f"gcs_folder: {target['gcs_folder']}")

        target.update(source)


def load_session_state(callback_context: CallbackContext):
    """
    Sets up the initial state.
    Set this as a callback as before_agent_call of the `root_agent`.
    This gets called before the system instruction is constructed.

    Args:
        callback_context: The callback context.
    """
    # Correlation line: ties this run to a session transcript. The frontend mints
    # a throwaway user_id per submission, so without this a UI failure can't be
    # traced back to a specific session for post-mortem inspection.
    logging.info(
        "run start: agent=%s invocation=%s session=%s user=%s",
        callback_context.agent_name,
        callback_context.invocation_id,
        callback_context.session.id,
        callback_context.user_id,
    )

    data = {}
    data["state"] = {
        "brand": "",  # BRAND,
        "target_product": "",  # TARGET_PRODUCT,
        "target_audience": "",  # TARGET_AUDIENCE,
        "key_selling_points": "",  # KEY_SELLING_POINT,
        "target_search_trends": {"target_search_trends": []},
    }

    _set_initial_states(data["state"], callback_context.state)


def rate_limit_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> None:
    # pylint: disable=unused-argument
    """Callback function that implements a query rate limit.

    Args:
      callback_context: A CallbackContext object representing the active
              callback context.
      llm_request: A LlmRequest object representing the active LLM request.
    """
    now = time.time()
    if "timer_start" not in callback_context.state:
        callback_context.state["timer_start"] = now
        callback_context.state["request_count"] = 1
        logging.debug(
            "rate_limit_callback [timestamp: %i, req_count: 1, elapsed_secs: 0]",
            now,
        )
        return

    request_count = callback_context.state["request_count"] + 1
    elapsed_secs = now - callback_context.state["timer_start"]
    logging.debug(
        "rate_limit_callback [timestamp: %i, request_count: %i, elapsed_secs: %i]",
        now,
        request_count,
        elapsed_secs,
    )

    if request_count > config.rpm_quota:
        delay = config.rate_limit_seconds - elapsed_secs + 1
        if delay > 0:
            logging.debug("Sleeping for %i seconds", delay)
            time.sleep(delay)
        callback_context.state["timer_start"] = now
        callback_context.state["request_count"] = 1
    else:
        callback_context.state["request_count"] = request_count

    return


def _describe_state_value(value: Any) -> str:
    """Compact, non-verbose description of a state value's presence."""
    if value is None:
        return "MISSING"
    if isinstance(value, str):
        return f"present(len={len(value)})" if value.strip() else "empty"
    if isinstance(value, (list, dict)):
        return f"present({type(value).__name__}, n={len(value)})"
    return "present"


def log_final_state_summary(callback_context: CallbackContext):
    """Log the presence of trend_scout's load-bearing state keys at end of run.

    Set as the root agent's `after_agent_callback`. Makes it trivial to see
    *where* a run stalled: e.g. `raw_gtrends` present but `info_gtrends` MISSING
    means the understand step was skipped or emitted an empty turn — the exact
    ambiguity that was impossible to resolve from logs alone during the
    2026-07-14 incident. Also surfaces any `*__retry_exhausted` markers left by
    RetryUntilKeyAgent.
    """
    state = callback_context.state
    keys = ("raw_gtrends", "info_gtrends", "selected_gtrends")
    summary = {k: _describe_state_value(state.get(k)) for k in keys}
    exhausted = sorted(k for k in state if k.endswith("__retry_exhausted"))
    logging.info(
        "trend_scout final state [invocation=%s]: %s%s",
        callback_context.invocation_id,
        summary,
        f" retry_exhausted={exhausted}" if exhausted else "",
    )


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
