"""callbacks - currently exploring how these work by observing log output"""

from typing import Dict, Any
import os, json, time
import pandas as pd
import logging

logging.basicConfig(level=logging.INFO)

from google.genai import types
from google.adk.sessions.state import State
from google.adk.models.llm_request import LlmRequest
from google.adk.agents.callback_context import CallbackContext

from .config import config, setup_config


# Get the cloud storage bucket from the environment variable
try:
    GCS_BUCKET = os.environ["BUCKET"]
except KeyError:
    raise Exception("BUCKET environment variable not set")


# get current working directory
CWD = os.getcwd()
logging.info(f"The current working directory is: {CWD}")


# get initial session state json
SESSION_STATE_JSON_PATH = os.getenv("SESSION_STATE_JSON_PATH", default=None)
logging.info(f"\n\n`SESSION_STATE_JSON_PATH`: {SESSION_STATE_JSON_PATH}\n\n")

# TODO: this is a short term fix for deployment to agent space
if SESSION_STATE_JSON_PATH:
    PROFILE_PATH = f"{CWD}/trend_agent/shared_libraries/profiles"
    FULL_JSON_PATH = os.path.join(PROFILE_PATH, SESSION_STATE_JSON_PATH)
else:
    FULL_JSON_PATH = None


def _set_initial_states(source: Dict[str, Any], target: State | dict[str, Any]):
    """
    Setting the initial session state given a JSON object of states.

    Args:
        source: A JSON object of states.
        target: The session state object to insert into.
    """
    if setup_config.state_init not in target:
        target[setup_config.state_init] = True
        target["gcs_folder"] = pd.Timestamp.utcnow().strftime("%Y_%m_%d_%H_%M")

        target.update(source)


def _load_session_state(callback_context: CallbackContext):
    """
    Sets up the initial state.
    Set this as a callback as before_agent_call of the `root_agent`.
    This gets called before the system instruction is constructed.

    Args:
        callback_context: The callback context.
    """
    data = {}
    if FULL_JSON_PATH:
        # resp = requests.get(FULL_JSON_PATH)
        # data = json.loads(resp.text)
        with open(FULL_JSON_PATH, "r") as file:
            data = json.load(file)
            logging.info(f"\n\nLoading Initial State: {data}\n\n")
    else:
        data = setup_config.empty_session_state
        logging.info(f"\n\nLoading Initial State (empty): {data}\n\n")

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
            "rate_limit_callback [timestamp: %i, req_count: 1, " "elapsed_secs: 0]",
            now,
        )
        return

    request_count = callback_context.state["request_count"] + 1
    elapsed_secs = now - callback_context.state["timer_start"]
    logging.debug(
        "rate_limit_callback [timestamp: %i, request_count: %i," " elapsed_secs: %i]",
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
