from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.sessions.state import State

from agent_common import observability
from agent_common.rate_limit import build_rate_limit_callback
from agent_common.state import seed_initial_state

from .config import config

# Shared debugging-observability callbacks (extracted to agent_common in WS3).
# Re-exported here so trend_scout/agent.py keeps referencing `callbacks.<name>`.
log_empty_turn_finish_reason = observability.log_empty_turn_finish_reason
log_final_state_summary = observability.make_final_state_summary(
    "trend_scout", ("raw_gtrends", "info_gtrends", "selected_gtrends")
)


def _normalize_trend_scope(target: State | dict[str, Any]) -> None:
    """Normalize ISO-3166-1 alpha-2 country + optional DMA/region in session state.

    Preserves caller-seeded values across turn-1 ``_set_initial_states`` while
    defaulting unset runs to US national trends.
    """
    raw_country = target.get("trend_country_code")
    if not isinstance(raw_country, str) or not raw_country.strip():
        target["trend_country_code"] = "US"
    else:
        target["trend_country_code"] = raw_country.strip().upper()

    raw_region = target.get("trend_region")
    if not isinstance(raw_region, str):
        target["trend_region"] = ""
    else:
        target["trend_region"] = " ".join(raw_region.split())


def _set_initial_states(source: dict[str, Any], target: State | dict[str, Any]):
    """
    Setting the initial session state given a JSON object of states.

    Args:
        source: A JSON object of states.
        target: The session state object to insert into.
    """
    seed_initial_state(
        source,
        target,
        state_init_key=config.state_init,
        gcs_bucket=config.GCS_BUCKET,
        agent_output_dir="trawler_output",
        timezone=config.trend_timezone,
    )
    _normalize_trend_scope(target)


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
        "brand": "",
        "target_product": "",
        "target_audience": "",
        "key_selling_points": "",
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

    _normalize_trend_scope(callback_context.state)


# Shared query rate limiter (agent_common). Built with trend_scout's config so
# `callbacks.rate_limit_callback` keeps the same name/signature for the
# before_model_callback wiring in agent.py.
rate_limit_callback = build_rate_limit_callback(config)
