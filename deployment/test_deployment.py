"""Test deployment of Trend Trawler Agents."""

import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

import dotenv

# Add the project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import agentplatform

from deployment.deploy_agent import AGENT_DEPLOY_SPECS, engine_env_key

# ==============================
# config
# ==============================
# Side effects (.env load, logging config, argv parsing, client construction)
# happen only in main(), so tests can import this module and build_parser().
ENV_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))


def build_test_query() -> str:
    """The campaign prompt sent to the deployed agent (read from .env)."""
    return f"""Brand: {os.getenv("BRAND")}
Target Product: {os.getenv("TARGET_PRODUCT")}
Key Selling Point(s): {os.getenv("KEY_SELLING_POINT")}
Target Audience: {os.getenv("TARGET_AUDIENCE")}
Target Search Trend: {os.getenv("TARGET_SEARCH_TREND")}
"""


# Campaign state key -> .env var, seeded via createSession state for the agents
# whose state init preserves caller-seeded values (trend_scout's overwrites them).
_CAMPAIGN_STATE_ENV = {
    "brand": "BRAND",
    "target_product": "TARGET_PRODUCT",
    "key_selling_points": "KEY_SELLING_POINT",
    "target_audience": "TARGET_AUDIENCE",
    "target_search_trends": "TARGET_SEARCH_TREND",
}
_STATE_SEEDED_AGENTS = ("creative_agent", "interactive_creative")


def build_test_state(agent: str) -> dict[str, str] | None:
    """The initial session state for a test run (None = create without state).

    The creative agents read the seeded campaign fields deterministically; the
    query message above is kept as a readable echo. Unset env vars are omitted.
    """
    if agent not in _STATE_SEEDED_AGENTS:
        return None
    return {
        key: value
        for key, env in _CAMPAIGN_STATE_ENV.items()
        if (value := os.getenv(env))
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="An asyncio application with command-line arguments."
    )
    parser.add_argument(
        "--user_id",
        type=str,
        default=None,
        help="User ID (can be any string).",
        required=True,
    )
    parser.add_argument(
        "--agent",
        choices=list(AGENT_DEPLOY_SPECS),
        default=None,
        help="name of deployed agent to test",
        required=True,
    )
    return parser


def pretty_print_event(event):
    """Pretty prints an event with truncation for long content."""
    if not event.get("content"):
        logging.info(f"[{event.get('author', 'unknown')}]: {event}")
        return

    author = event.get("author", "unknown")
    parts = event["content"].get("parts") or []

    for part in parts:
        # 2.x streams snake_case dicts; accept camelCase too (integration_test's
        # _function_call_names does the same for function_call).
        func_call = part.get("function_call") or part.get("functionCall")
        func_response = part.get("function_response") or part.get("functionResponse")
        if part.get("text"):
            text = part["text"]
            logging.info(f"[{author}]: {text}")
        elif func_call:
            logging.info(
                f"[{author}]: Function call: {func_call.get('name', 'unknown')}"
            )
            # Truncate args if too long
            args = json.dumps(func_call.get("args", {}))
            if len(args) > 100:
                args = args[:97] + "..."
            logging.info(f"  Args: {args}")
        elif func_response:
            logging.info(
                f"[{author}]: Function response: {func_response.get('name', 'unknown')}"
            )
            # Truncate response if too long
            response = json.dumps(func_response.get("response", {}))
            if len(response) > 100:
                response = response[:97] + "..."
            logging.info(f"  Response: {response}")


# function to interact with remote agent
async def async_send_message(remote_agent, user_id, session) -> None:
    """Send a message to the deployed agent."""

    # Clear events for each new query
    events = []

    try:
        async for event in remote_agent.async_stream_query(
            user_id=user_id,
            session_id=session["id"],
            message=build_test_query(),  # user_input
        ):
            events.append(event)
            pretty_print_event(event)

    except Exception as e:
        logging.error(f"Error during streaming: {type(e).__name__}: {e}")
        # Propagate so a broken deployment surfaces as a failure, mirroring the
        # worker path (cloud_functions/creative_fanout/main.py).
        raise


@asynccontextmanager
async def agent_session(remote_agent, user_id, state=None):
    """Create → yield → delete an Agent Engine session with ONE user_id.

    Local mirror of cloud_functions/creative_fanout/session.agent_session
    (deployment/ isn't bundled with the worker, so it can't import it). Deleting
    with the SAME user_id the session was created under avoids Agent Engine's
    `FAILED_PRECONDITION: Session <id> does not belong to user <...>`.
    ``state`` (optional) is the initial session state.
    """
    if state is None:
        session = await remote_agent.async_create_session(user_id=user_id)
    else:
        session = await remote_agent.async_create_session(user_id=user_id, state=state)
    logging.info(f"Created session {session['id']} for user ID: {user_id}")
    try:
        yield session
    finally:
        await remote_agent.async_delete_session(
            user_id=user_id, session_id=session["id"]
        )
        logging.info(f"Deleted session {session['id']} for user ID: {user_id}")


async def main() -> None:  # pylint: disable=unused-argument
    """Main function that uses the defined flags."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    dotenv.load_dotenv(dotenv_path=ENV_FILE_PATH)
    args = build_parser().parse_args()

    # Agent Engine is a *regional* resource, so it uses GCP_REGION (us-central1) —
    # NOT GOOGLE_CLOUD_LOCATION, which is set to `global` for the gemini-3.x models.
    client = agentplatform.Client(
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=os.getenv("GCP_REGION", "us-central1"),
    )  # pyright: ignore[reportCallIssue]

    # get instance of agent
    logging.info("\n\nGetting Agent Engine Runtime...\n\n")
    if not args.agent:
        logging.error("Error: --agent is required for the create operation.")
        return
    env_key = engine_env_key(args.agent)
    remote_agent = client.runtimes.get(name=os.getenv(env_key))
    logging.info(f"\n\nremote_agent: {remote_agent}")

    # get session — create → stream → delete under one user_id (delete-on-error).
    logging.info(f"\n\nCreating session for user ID: {args.user_id}...\n\n")
    async with agent_session(
        remote_agent, args.user_id, state=build_test_state(args.agent)
    ) as session:
        logging.info(session)
        # long running op
        await async_send_message(
            remote_agent=remote_agent, user_id=args.user_id, session=session
        )


if __name__ == "__main__":
    asyncio.run(main())
