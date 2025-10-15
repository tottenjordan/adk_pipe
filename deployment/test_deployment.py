"""Test deployment of Trend Trawler Agents."""

import os
import sys
import dotenv
import asyncio
import logging
import warnings
from absl import app, flags

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


# define flags from command line args
FLAGS = flags.FLAGS
flags.DEFINE_enum(
    name="agent",
    default=None,
    enum_values=["trend_trawler", "creative_agent"],
    help="name of agent to deploy",
    # required=True,
)
flags.DEFINE_string("user_id", None, "User ID (can be any string).", required=True)


# vertex ai SDK client
client = vertexai.Client(
    project=os.getenv("GOOGLE_CLOUD_PROJECT"),
    location=os.getenv("GOOGLE_CLOUD_LOCATION"),
)  # pyright: ignore[reportCallIssue]


# function to interact with remote agent
async def send_message(remote_agent, user_id) -> None:
    """Send a message to the deployed agent."""

    session = await remote_agent.async_create_session(user_id=user_id)
    logging.info(f"\n\nCreated session for user ID: {user_id}\n\n")
    logging.info(session)
    logging.info("\n\nType 'quit' to exit.\n\n")

    events = []
    while True:
        user_input = input("Input: ")
        if user_input == "quit":
            break

        async for event in remote_agent.async_stream_query(
            user_id=user_id, session_id=session["id"], message=user_input
        ):

            events.append(event)
            # logging.info(event) # full event stream i.e., agent's thought process
            

        # Extract just the final text response
        final_text_responses = [
            e for e in events
            if e.get("content", {}).get("parts", [{}])[0].get("text")
            and not e.get("content", {}).get("parts", [{}])[0].get("function_call")
        ]
        if final_text_responses:
            logging.info("\n\n--- Final Response ---\n\n")
            logging.info(final_text_responses[0]["content"]["parts"][0]["text"])

    await remote_agent.async_delete_session(
        user_id=FLAGS.user_id, session_id=session["id"]
    )
    logging.info(f"Deleted session for user ID: {FLAGS.user_id}")


def main(argv) -> None:  # pylint: disable=unused-argument
    """Main function that uses the defined flags."""
    del argv

    # get instance of agent
    logging.info(f"\n\nGetting Agent Engine Runtime...\n\n")
    if not FLAGS.agent:
        logging.error("Error: --agent is required for the create operation.")
        return
    if FLAGS.agent == "trend_trawler":
        remote_agent = client.agent_engines.get(
            name=os.getenv("TRAWLER_AGENT_ENGINE_ID")
        )

    elif FLAGS.agent == "creative_agent":
        remote_agent = client.agent_engines.get(
            name=os.getenv("CREATIVE_AGENT_ENGINE_ID")
        )

    logging.info(f"\n\nremote_agent: {remote_agent}")
    asyncio.run(send_message(remote_agent=remote_agent, user_id=FLAGS.user_id))


if __name__ == "__main__":
    app.run(main)
