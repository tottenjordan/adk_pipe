"""Deployment script for Trend Trawler Agents."""

import os
import sys
import dotenv
import logging
import pandas as pd
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


# load .env file
ENV_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
dotenv.load_dotenv(dotenv_path=ENV_FILE_PATH)

ENV_VAR_DICT = {
    "GOOGLE_GENAI_USE_VERTEXAI": os.getenv("GOOGLE_GENAI_USE_VERTEXAI"),
    # "GOOGLE_CLOUD_PROJECT": os.getenv("GOOGLE_CLOUD_PROJECT"),
    # "GOOGLE_CLOUD_LOCATION": os.getenv("GOOGLE_CLOUD_LOCATION"),
    "GOOGLE_CLOUD_PROJECT_NUMBER": os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER"),
    "GOOGLE_CLOUD_STORAGE_BUCKET": os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET"),
    "BUCKET": os.getenv("BUCKET"),
    "BQ_PROJECT_ID": os.getenv("BQ_PROJECT_ID"),
    "BQ_DATASET_ID": os.getenv("BQ_DATASET_ID"),
    "BQ_TABLE_TARGETS": os.getenv("BQ_TABLE_TARGETS"),
    "BQ_TABLE_CREATIVES": os.getenv("BQ_TABLE_CREATIVES"),
    "BQ_TABLE_ALL_TRENDS": os.getenv("BQ_TABLE_ALL_TRENDS"),
}


# define flags from command line args
FLAGS = flags.FLAGS
flags.DEFINE_string(name="version", default=None, help="version namespace")
flags.DEFINE_enum(
    name="agent",
    default=None,
    enum_values=["trend_trawler", "creative_agent"],
    help="name of agent to deploy",
    # required=True,
)
flags.DEFINE_string(
    "resource_id", None, "Agent Engine resource id for deletion.", short_name="r"
)

# action to execute
flags.DEFINE_bool("list", False, "list all agent engine instances.")
flags.DEFINE_bool("create", False, "create new agent engine runtime (deployment)")
flags.DEFINE_bool("delete", False, "delete existing agent engine instance")
flags.mark_bool_flags_as_mutual_exclusive(["create", "delete", "list"])


# vertex ai SDK client
client = vertexai.Client(
    project=os.getenv("GOOGLE_CLOUD_PROJECT"),
    location=os.getenv("GOOGLE_CLOUD_LOCATION"),
)  # pyright: ignore[reportCallIssue]


# Function to update the .env file
def update_env_file(prefix, agent_engine_id, env_file_path):
    """Updates the .env file with the agent engine ID."""
    try:
        KEY_NAME = f"{prefix}_AGENT_ENGINE_ID"
        dotenv.set_key(env_file_path, KEY_NAME, agent_engine_id)
        logging.info(f"Updated {KEY_NAME} in {env_file_path} to {agent_engine_id}")
    except Exception as e:
        logging.info(f"Error updating .env file: {e}")


# ==============================
# CRUD ops
# ==============================
# TODO: add op for update()


# create deployment: trend_trawler
def deploy_trawler(version: str) -> None:
    """Creates and deploys `trend_trawler` Agent to Vertex AI Agent Engine Runtime."""

    from trend_trawler.agent import root_agent

    try:
        logging.info(f"Deploying `trend_trawler` agent...")
        remote_agent = client.agent_engines.create(
            agent=root_agent,
            config={
                "requirements": "./requirements.txt",
                "extra_packages": ["./trend_trawler"],
                "staging_bucket": f"gs://{os.getenv('GOOGLE_CLOUD_STORAGE_BUCKET')}",
                "gcs_dir_name": f"adk-pipe/trawler/{version}/staging",
                "display_name": "trend-trawler",
                "description": root_agent.description,
                "env_vars": ENV_VAR_DICT,
                # "service_account": SERVICE_ACCOUNT,
                "min_instances": 1,
                "max_instances": 100,
                "resource_limits": {"cpu": "4", "memory": "8Gi"},
                "container_concurrency": 9,  # recommended value is 2 * cpu + 1
            },
        )
        logging.info(
            f"\n\nSuccessfully created remote agent: {remote_agent.api_resource.name}\n\n"
        )
        update_env_file(
            prefix="TRAWLER",
            agent_engine_id=remote_agent.api_resource.name,
            env_file_path=ENV_FILE_PATH,
        )
    except Exception as e:
        logging.exception(f"Error deploying agent to Agent Engine Runtime: {e}")


# create deployment: creative_agent
def deploy_creative_agent(version: str) -> None:
    """Creates and deploys `creative_agent` Agent to Vertex AI Agent Engine Runtime."""

    from creative_agent.agent import root_agent

    try:
        logging.info(f"Deploying `creative_agent` agent...")
        remote_agent = client.agent_engines.create(
            agent=root_agent,
            config={
                "requirements": "./requirements.txt",
                "extra_packages": ["./creative_agent"],
                "staging_bucket": f"gs://{os.getenv('GOOGLE_CLOUD_STORAGE_BUCKET')}",
                "gcs_dir_name": f"adk-pipe/creative/{version}/staging",
                "display_name": "creative-trend",
                "description": root_agent.description,
                "env_vars": ENV_VAR_DICT,
                # "service_account": SERVICE_ACCOUNT,
                "min_instances": 1,
                "max_instances": 100,
                "resource_limits": {"cpu": "4", "memory": "8Gi"},
                "container_concurrency": 9,  # recommended value is 2 * cpu + 1
            },
        )
        logging.info(
            f"\n\nSuccessfully created remote agent: {remote_agent.api_resource.name}\n\n"
        )
        update_env_file(
            prefix="CREATIVE",
            agent_engine_id=remote_agent.api_resource.name,
            env_file_path=ENV_FILE_PATH,
        )
    except Exception as e:
        logging.exception(f"Error deploying agent to Agent Engine Runtime: {e}")


# list agents
def list_agents() -> None:
    """Lists all Agent Engine Runtimes in the Project and Location"""
    logging.info("Listing all deployed Agent Engine Runtimes...")
    remote_agents = client.agent_engines.list()
    if not remote_agents:
        logging.info("No agents found.")
        return

    template_lines = [
        '{agent.name} ("{agent.display_name}")',
        "- Create time: {agent.create_time}",
        "- Update time: {agent.update_time}",
        "- Description: {agent.description}",
    ]
    template = "\n".join(template_lines)

    remote_agents_string = "\n\n".join(
        template.format(agent=agent) for agent in remote_agents
    )
    logging.info(f"\nAll remote agents:\n{remote_agents_string}")


def delete(resource_id: str) -> None:
    """Deletes an existing agent engine."""
    logging.info(f"Attempting to delete agent: {resource_id}")

    PROJECT_NUM = os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER")
    LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")
    RESOURCE_NAME = (
        f"projects/{PROJECT_NUM}/locations/{LOCATION}/reasoningEngines/{resource_id}"
    )

    remote_agent = client.agent_engines.get(name=RESOURCE_NAME)
    remote_agent.delete(force=True)
    logging.info(f"Successfully deleted remote agent: {resource_id}")


def main(argv):
    """Main function that uses the defined flags."""
    del argv

    if FLAGS.version is None:
        FLAGS.version = pd.Timestamp.utcnow().strftime("%Y_%m_%d_%H_%M")
    logging.info(f"version: {FLAGS.version}")

    if FLAGS.list:
        list_agents()

    elif FLAGS.create:
        if not FLAGS.agent:
            logging.error("Error: --agent is required for the create operation.")
            return
        if FLAGS.agent == "trend_trawler":
            logging.info(f"Creating Agent Engine Runtime for `trend_trawler`...")
            deploy_trawler(version=FLAGS.version)

        elif FLAGS.agent == "creative_agent":
            logging.info(f"Creating Agent Engine Runtime for `creative_agent`...")
            deploy_creative_agent(version=FLAGS.version)

    elif FLAGS.delete:
        if not FLAGS.resource_id:
            logging.error("Error: --resource_id is required for the delete operation.")
            return
        delete(resource_id=FLAGS.resource_id)

    else:
        logging.info("No command specified. Use --create, --delete, or --list.")


if __name__ == "__main__":
    app.run(main)
