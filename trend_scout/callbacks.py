import uuid
import logging
import warnings
import pandas as pd
from typing import Dict, Any

from google.adk.sessions.state import State
from google.adk.agents.callback_context import CallbackContext

from agent_common import observability
from agent_common.rate_limit import build_rate_limit_callback

from .config import config


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


# Shared debugging-observability callbacks (extracted to agent_common in WS3).
# Re-exported here so trend_scout/agent.py keeps referencing `callbacks.<name>`.
log_empty_turn_finish_reason = observability.log_empty_turn_finish_reason
log_final_state_summary = observability.make_final_state_summary(
    "trend_scout", ("raw_gtrends", "info_gtrends", "selected_gtrends")
)


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
    observability.log_run_start(callback_context)

    data = {}
    data["state"] = {
        "brand": "",  # BRAND,
        "target_product": "",  # TARGET_PRODUCT,
        "target_audience": "",  # TARGET_AUDIENCE,
        "key_selling_points": "",  # KEY_SELLING_POINT,
        "target_search_trends": {"target_search_trends": []},
    }

    _set_initial_states(data["state"], callback_context.state)

    # Opt-in human checkpoint for trend selection (default OFF). Seeded only when
    # absent so the `{interactive_trend_pick?}` instruction var is always defined
    # and a non-interactive run is byte-for-byte unaffected — while a caller that
    # opted in (passing `interactive_trend_pick=True` in the initial session
    # state) keeps its value (unlike the metadata keys above, which are seeded
    # blank and filled later via `memorize`).
    if "interactive_trend_pick" not in callback_context.state:
        callback_context.state["interactive_trend_pick"] = False


# Shared query rate limiter (agent_common). Built with trend_scout's config so
# `callbacks.rate_limit_callback` keeps the same name/signature for the
# before_model_callback wiring in agent.py.
rate_limit_callback = build_rate_limit_callback(config)
