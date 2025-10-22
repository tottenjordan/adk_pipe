"""Test deployment of Trend Trawler Agents."""

import os
import sys
import json
import dotenv
import asyncio
import logging
import warnings
import argparse

# from absl import app, flags

# Add the project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import vertexai


# ==============================
# config
# ==============================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")

# load .env file
ENV_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
dotenv.load_dotenv(dotenv_path=ENV_FILE_PATH)


TEST_QUERY = f"""Brand: {os.getenv("BRAND")}
Target Product: {os.getenv("TARGET_PRODUCT")}
Key Selling Point(s): {os.getenv("KEY_SELLING_POINT")}
Target Audience: {os.getenv("TARGET_AUDIENCE")}
Target Search Trend: {os.getenv("TARGET_SEARCH_TREND")}
"""


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
    choices=["trend_trawler", "creative_agent"],
    default=None,
    help="name of agent to deploy",
    required=True,
)
args = parser.parse_args()


# vertex ai SDK client
client = vertexai.Client(
    project=os.getenv("GOOGLE_CLOUD_PROJECT"),
    location=os.getenv("GOOGLE_CLOUD_LOCATION"),
)  # pyright: ignore[reportCallIssue]


def pretty_print_event(event):
    """Pretty prints an event with truncation for long content."""
    if "content" not in event:
        logging.info(f"[{event.get('author', 'unknown')}]: {event}")
        return

    author = event.get("author", "unknown")
    parts = event["content"].get("parts", [])

    for part in parts:
        if "text" in part:
            text = part["text"]
            logging.info(f"[{author}]: {text}")
        elif "functionCall" in part:
            func_call = part["functionCall"]
            logging.info(
                f"[{author}]: Function call: {func_call.get('name', 'unknown')}"
            )
            # Truncate args if too long
            args = json.dumps(func_call.get("args", {}))
            if len(args) > 100:
                args = args[:97] + "..."
            logging.info(f"  Args: {args}")
        elif "functionResponse" in part:
            func_response = part["functionResponse"]
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
            message=TEST_QUERY,  # user_input
        ):
            events.append(event)
            pretty_print_event(event)

    except Exception as e:
        logging.error(f"Error during streaming: {type(e).__name__}: {e}")


async def my_delete_task(remote_agent, session):
    logging.info(f"Delete task starting with agent: {remote_agent}...")
    await remote_agent.async_delete_session(
        user_id=args.user_id, session_id=session["id"]
    )
    logging.info(f"Deleted session for user ID: {args.user_id}")


async def main() -> None:  # pylint: disable=unused-argument
    """Main function that uses the defined flags."""

    # get instance of agent
    logging.info(f"\n\nGetting Agent Engine Runtime...\n\n")
    if not args.agent:
        logging.error("Error: --agent is required for the create operation.")
        return
    if args.agent == "trend_trawler":
        remote_agent = client.agent_engines.get(
            name=os.getenv("TRAWLER_AGENT_ENGINE_ID")
        )
    elif args.agent == "creative_agent":
        remote_agent = client.agent_engines.get(
            name=os.getenv("CREATIVE_AGENT_ENGINE_ID")
        )
    logging.info(f"\n\nremote_agent: {remote_agent}")

    # get session
    logging.info(f"\n\nCreating session for user ID: {args.user_id}...\n\n")
    session = await remote_agent.async_create_session(user_id=args.user_id)
    logging.info(session)

    # long running op
    await async_send_message(
        remote_agent=remote_agent, user_id=args.user_id, session=session
    )
    await my_delete_task(remote_agent=remote_agent, session=session)


if __name__ == "__main__":
    asyncio.run(main())
