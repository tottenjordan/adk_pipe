"""Deployment script for Trend Trawler Agents."""

import logging
import os
import sys
from typing import TYPE_CHECKING, cast

import dotenv
import pandas as pd
from absl import app, flags

# Add the project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import agentplatform

if TYPE_CHECKING:
    from google.adk.agents import BaseAgent
    from google.adk.apps import App

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
    # google-genai/ADK renamed GOOGLE_GENAI_USE_VERTEXAI (now deprecated); fall back
    # to the old name so a not-yet-updated local .env still deploys a Vertex engine.
    "GOOGLE_GENAI_USE_ENTERPRISE": os.getenv("GOOGLE_GENAI_USE_ENTERPRISE")
    or os.getenv("GOOGLE_GENAI_USE_VERTEXAI"),
    # NOTE: GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION are RESERVED by Agent
    # Engine (it rejects them with `FAILED_PRECONDITION ... is reserved`) and are
    # auto-injected into the runtime as the engine's project + region. Because the
    # injected location is regional (us-central1) but the gemini-3.x models are
    # served only from `global`, the model location is pinned in code instead —
    # see MODEL_LOCATION in the agent configs — NOT via these env vars.
    "GOOGLE_CLOUD_PROJECT_NUMBER": os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER"),
    "GOOGLE_CLOUD_STORAGE_BUCKET": os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET"),
    "BQ_PROJECT_ID": os.getenv("BQ_PROJECT_ID"),
    "BQ_DATASET_ID": os.getenv("BQ_DATASET_ID"),
    "BQ_TABLE_TARGETS": os.getenv("BQ_TABLE_TARGETS"),
    "BQ_TABLE_CREATIVES": os.getenv("BQ_TABLE_CREATIVES"),
    "BQ_TABLE_EVALS": os.getenv("BQ_TABLE_EVALS"),
    # creative_agent brief_gate revision budget (0..2, default 1; see
    # creative_agent/config.py). Defaulted so the engine never gets a None.
    "BRIEF_REVISION_ROUNDS": os.getenv("BRIEF_REVISION_ROUNDS", "1"),
    # creative_agent copy_gate revision budget (0..2, default 1; same contract).
    "COPY_REVISION_ROUNDS": os.getenv("COPY_REVISION_ROUNDS", "1"),
    # creative_agent concept_gate fix budget (0..2, default 1; same contract).
    "CONCEPT_REVISION_ROUNDS": os.getenv("CONCEPT_REVISION_ROUNDS", "1"),
    # creative_agent post-render image QA (agent_common/config.py): kill switch
    # (default on), per-image re-render budget (0..2, default 1), per-run
    # re-render cap (0..8, default 2) and the vision model.
    "IMAGE_QA_ENABLED": os.getenv("IMAGE_QA_ENABLED", "true"),
    "IMAGE_QA_MAX_RERENDERS": os.getenv("IMAGE_QA_MAX_RERENDERS", "1"),
    "IMAGE_QA_MAX_RERENDERS_PER_RUN": os.getenv("IMAGE_QA_MAX_RERENDERS_PER_RUN", "2"),
    "IMAGE_QA_MODEL": os.getenv("IMAGE_QA_MODEL") or "gemini-3.8-flash",
    # creative_agent brand history (creative_agent/brand_history.py): kill
    # switch (default on) and how many past runs to read (0..20, default 5).
    "BRAND_HISTORY_ENABLED": os.getenv("BRAND_HISTORY_ENABLED", "true"),
    "BRAND_HISTORY_RUNS": os.getenv("BRAND_HISTORY_RUNS", "5"),
    # creative_agent rating learning (creative_agent/rating_signals.py; opt-in
    # per run): global kill switch (default on = opt-in allowed), enabled
    # effects, minimum sample sizes, look-back window, and the ratings table.
    "RATING_LEARNING_ENABLED": os.getenv("RATING_LEARNING_ENABLED", "true"),
    "RATING_LEARNING_EFFECTS": os.getenv(
        "RATING_LEARNING_EFFECTS", "guidance,styles,checks"
    ),
    "RATING_LEARNING_MIN_RATINGS": os.getenv("RATING_LEARNING_MIN_RATINGS", "8"),
    "RATING_STYLE_MIN": os.getenv("RATING_STYLE_MIN", "3"),
    "RATING_REASON_MIN": os.getenv("RATING_REASON_MIN", "3"),
    "RATING_LEARNING_WINDOW_DAYS": os.getenv("RATING_LEARNING_WINDOW_DAYS", "90"),
    "BQ_TABLE_RATINGS": os.getenv("BQ_TABLE_RATINGS") or "creative_ratings",
    # creative_agent person casting (creative_agent/config.py): the style families
    # a cast concept may use and the cap on cast concepts per set (0..4, default
    # 2; 0 = casting off). Defaults from the calibration spike.
    "PERSON_SAFE_STYLES": os.getenv("PERSON_SAFE_STYLES")
    or "Candid 35mm film photo,Photoreal / editorial,Cinematic film still",
    "MAX_CAST_CONCEPTS": os.getenv("MAX_CAST_CONCEPTS", "2"),
    # Per-request timeout for flash / lite model calls (agent_common/genai_retry.py;
    # default 90, clamped 30..900, 0 = use MODEL_REQUEST_TIMEOUT_SECONDS). The ADK
    # agent models bake it in at deploy (pickled); the lazily built image-QA
    # client reads it in the engine runtime, hence shipped here.
    "FLASH_MODEL_REQUEST_TIMEOUT_SECONDS": os.getenv(
        "FLASH_MODEL_REQUEST_TIMEOUT_SECONDS", "90"
    ),
}


def build_env_vars(enable_tracing: bool = False) -> dict[str, str | None]:
    """Return a copy of ENV_VAR_DICT, plus the Agent Engine telemetry flag if asked.

    With ``AdkApp(enable_tracing=None)`` (our construction), Agent Engine turns on
    Cloud Trace export when ``GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true``.
    We deliberately do NOT pass ``enable_tracing=True`` to AdkApp: that also forces
    prompt/response content capture into the spans. ADK's own spans capture the
    full LLM request/response by default too, so tracing also sets
    ``ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false`` (as ``adk deploy`` does).
    """
    env = {**ENV_VAR_DICT}
    if enable_tracing:
        env["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] = "true"
        env["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = "false"
    return env


# ==============================
# per-agent deploy definitions
# ==============================
# Single source of truth for what each agent bundles into its Agent Engine
# runtime. Agent Engine *copies* these dirs into the container (it does NOT
# pip-install the repo), so a package a root_agent imports at runtime MUST be
# listed here or the engine fails to start (e.g. `No module named 'creative_eval'`).
# Derived from the real import graph:
#   trend_scout          -> agent_common
#   creative_agent       -> creative_eval, agent_common
#   interactive_creative -> creative_agent, creative_eval, agent_common
# Convention: the agent's own package is listed first, then its cross-package deps.
AGENT_EXTRA_PACKAGES = {
    "trend_scout": ["./trend_scout", "./agent_common"],
    "creative_agent": ["./creative_agent", "./creative_eval", "./agent_common"],
    "interactive_creative": [
        "./interactive_creative",
        "./creative_agent",
        "./creative_eval",
        "./agent_common",
    ],
}

# Per-agent deploy metadata: which module exposes root_agent, the .env key prefix
# (`<PREFIX>_AGENT_ENGINE_ID`), and the display/GCS-staging naming.
AGENT_DEPLOY_SPECS = {
    "trend_scout": {
        "module": "trend_scout.agent",
        "env_prefix": "SCOUT",
        "display_name": "trend-scout-agent",
        "gcs_subdir": "scout",
    },
    "creative_agent": {
        "module": "creative_agent.agent",
        "env_prefix": "CREATIVE",
        "display_name": "creative-trend-agent",
        "gcs_subdir": "creative",
    },
    "interactive_creative": {
        "module": "interactive_creative.agent",
        "env_prefix": "INTERACTIVE",
        "display_name": "interactive-creative-agent",
        "gcs_subdir": "interactive",
    },
}

# The deployable agent names — used for the --agent enum so the CLI and the
# bundle/spec maps can never drift apart.
AGENT_NAMES = tuple(AGENT_DEPLOY_SPECS)


# define flags from command line args
FLAGS = flags.FLAGS
flags.DEFINE_string(name="version", default=None, help="version namespace")
flags.DEFINE_enum(
    name="agent",
    default=None,
    enum_values=list(AGENT_NAMES),
    help="name of agent to deploy",
)
flags.DEFINE_string(
    "resource_id", None, "Agent Engine resource id for deletion.", short_name="r"
)

# action to execute
flags.DEFINE_bool("list", False, "list all agent engine instances.")
flags.DEFINE_bool("create", False, "create new agent engine runtime (deployment)")
flags.DEFINE_bool("delete", False, "delete existing agent engine instance")
flags.DEFINE_bool(
    "enable_tracing",
    False,
    "with --create: export Cloud Trace spans (sets "
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true; no prompt/response content).",
)
flags.mark_bool_flags_as_mutual_exclusive(["create", "delete", "list"])


# Agent Engine is a *regional* resource, so it uses GCP_REGION (us-central1) —
# NOT GOOGLE_CLOUD_LOCATION, which is set to `global` for the gemini-3.x models.
AGENT_ENGINE_LOCATION = os.getenv("GCP_REGION", "us-central1")

# Agent Runtime (agentplatform) client — created lazily so the module imports without GCP creds
# (mirrors the lazy-client pattern in cloud_functions/creative_fanout/main.py) and
# so unit tests can assert on the deploy mappings without a live client.
_client = None


def _get_client():
    """Return a cached Agent Runtime (agentplatform) client, creating it on first use."""
    global _client
    if _client is None:
        _client = agentplatform.Client(
            project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=AGENT_ENGINE_LOCATION,
        )  # pyright: ignore[reportCallIssue]
    return _client


def validate_extra_packages(packages: list[str]) -> None:
    """Assert every bundled package dir exists before calling runtimes.create.

    Guards against a typo'd or forgotten path silently shipping a runtime that
    fails to start with `No module named ...`.
    """
    missing = [p for p in packages if not os.path.isdir(os.path.join(project_root, p))]
    if missing:
        raise FileNotFoundError(
            f"extra_packages dirs not found under {project_root}: {missing}"
        )


def resolve_deploy_target(module) -> tuple[str, object]:
    """Pick what to hand AdkApp for an agent module: its App if it has one.

    Every agent module now exports an ADK ``App``, so all three deploy as
    ``AdkApp(app=...)``. ``trend_scout`` / ``interactive_creative`` carry
    ``ResumabilityConfig(is_resumable=True)``, which the ``LongRunningFunctionTool``
    review checkpoints need to pause/resume; ``creative_agent``'s App is
    non-resumable. All carry App-level ``plugins`` (the opt-in Model Armor screen,
    ``agent_common/safety.py``). Deploying the bare ``root_agent`` would silently
    drop both on Agent Engine.

    Returns ``(kind, target)`` where ``kind`` is the ``AdkApp`` keyword to use:
    ``("app", module.app)`` if it is an ``App``, else ``("agent", module.root_agent)``
    (an unrelated ``app`` attribute, e.g. a FastAPI app, is ignored).
    """
    # Lazy import keeps this module importable without ADK loaded up front.
    from google.adk.apps import App

    app_obj = getattr(module, "app", None)
    if isinstance(app_obj, App):
        return "app", app_obj
    return "agent", module.root_agent


def engine_env_key(name: str) -> str:
    """The .env key holding a deployed agent's Agent Engine resource ID.

    Single source of truth for the ``<PREFIX>_AGENT_ENGINE_ID`` format, shared by
    deploy (writes it) and the test/integration scripts (read it).
    """
    return f"{AGENT_DEPLOY_SPECS[name]['env_prefix']}_AGENT_ENGINE_ID"


# Function to update the .env file
def update_env_file(name: str, agent_engine_id: str, env_file_path: str):
    """Updates the .env file with the agent engine ID for agent ``name``."""
    KEY_NAME = engine_env_key(name)
    try:
        dotenv.set_key(env_file_path, KEY_NAME, agent_engine_id)
        logging.info(f"Updated {KEY_NAME} in {env_file_path} to {agent_engine_id}")
    except Exception as e:
        logging.info(f"Error updating .env file: {e}")


# ==============================
# CRUD ops
# ==============================
# TODO: add op for update()


# create deployment (unified): any agent in AGENT_DEPLOY_SPECS
def deploy_agent(name: str, version: str, enable_tracing: bool = False) -> None:
    """Creates and deploys an Agent to Vertex AI Agent Engine Runtime.

    Parameterized by AGENT_DEPLOY_SPECS (module/prefix/naming) and
    AGENT_EXTRA_PACKAGES (bundled dirs) so all agents share one deploy path and
    a new agent only needs an entry in those two maps. ``enable_tracing`` opts the
    engine into Cloud Trace export (see ``build_env_vars``).
    """
    import importlib

    spec = AGENT_DEPLOY_SPECS[name]
    extra_packages = AGENT_EXTRA_PACKAGES[name]
    validate_extra_packages(extra_packages)

    # Imported lazily (like the client) so this module stays importable offline.
    from agentplatform.frameworks import AdkApp

    module = importlib.import_module(spec["module"])
    root_agent = module.root_agent
    kind, target = resolve_deploy_target(module)
    # Explicit branch (not ``AdkApp(**{kind: target})``) so the keyword is typed.
    if kind == "app":
        adk_app = AdkApp(app=cast("App", target))
    else:
        adk_app = AdkApp(agent=cast("BaseAgent", target))

    try:
        logging.info(f"Deploying `{name}` agent...")
        remote_agent = _get_client().runtimes.create(
            agent=adk_app,
            config={
                "requirements": "./requirements.txt",
                "extra_packages": extra_packages,
                "staging_bucket": f"gs://{os.getenv('GOOGLE_CLOUD_STORAGE_BUCKET')}",
                "gcs_dir_name": f"adk-pipe/{spec['gcs_subdir']}/{version}/staging",
                "display_name": f"{spec['display_name']}-{version}",
                "description": root_agent.description,
                "env_vars": build_env_vars(enable_tracing),
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
            name=name,
            agent_engine_id=remote_agent.api_resource.name,
            env_file_path=ENV_FILE_PATH,
        )
    except Exception as e:
        logging.exception(f"Error deploying agent to Agent Engine Runtime: {e}")
        # Re-raise so main() can turn a failed deploy into a non-zero process exit
        # (a silent return here made `--create` failures exit 0 and mask CI/deploy
        # breakage).
        raise


# list agents
def list_agents() -> None:
    """Lists all Agent Engine Runtimes in the Project and Location"""
    logging.info("Listing all deployed Agent Engine Runtimes...")
    remote_agents = list(_get_client().runtimes.list())
    if not remote_agents:
        logging.info("No agents found.")
        return

    # The SDK exposes the resource fields on `.api_resource` (the create path
    # above reads `remote_agent.api_resource.name`); `agent.name` no longer
    # exists, so read every field off `.api_resource`.
    template_lines = [
        '{agent.api_resource.name} ("{agent.api_resource.display_name}")',
        "- Create time: {agent.api_resource.create_time}",
        "- Update time: {agent.api_resource.update_time}",
        "- Description: {agent.api_resource.description}",
    ]
    template = "\n".join(template_lines)

    remote_agents_string = "\n\n".join(
        template.format(agent=agent) for agent in remote_agents
    )
    logging.info(f"\nAll remote agents:\n{remote_agents_string}")


def delete(
    resource_id: str,
) -> None:
    """Deletes an existing agent engine."""
    logging.info(f"Attempting to delete agent: {resource_id}")

    PROJECT_NUM = os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER")
    RESOURCE_NAME = f"projects/{PROJECT_NUM}/locations/{AGENT_ENGINE_LOCATION}/reasoningEngines/{resource_id}"

    # runtimes.delete returns the long-running operation without polling it, so
    # report the request rather than claiming the engine is already gone.
    op = _get_client().runtimes.delete(name=RESOURCE_NAME, force=True)
    if op.error:
        logging.warning(
            f"Delete operation {op.name} for {resource_id} error: {op.error}"
        )
    logging.info(f"Delete requested for {resource_id} (operation {op.name})")


def main(argv):
    """Main function that uses the defined flags."""
    del argv

    if FLAGS.version is None:
        FLAGS.version = pd.Timestamp.now("UTC").strftime("%Y_%m_%d_%H_%M")
    logging.info(f"version: {FLAGS.version}")

    if FLAGS.list:
        list_agents()

    elif FLAGS.create:
        if not FLAGS.agent:
            logging.error("Error: --agent is required for the create operation.")
            sys.exit(1)
        logging.info(f"Creating Agent Engine Runtime for `{FLAGS.agent}`...")
        try:
            deploy_agent(
                name=FLAGS.agent,
                version=FLAGS.version,
                enable_tracing=FLAGS.enable_tracing,
            )
        except Exception:
            logging.error("Deploy failed; exiting non-zero.")
            sys.exit(1)

    elif FLAGS.delete:
        if not FLAGS.resource_id:
            logging.error("Error: --resource_id is required for the delete operation.")
            sys.exit(1)
        try:
            delete(resource_id=FLAGS.resource_id)
        except Exception:
            logging.exception("Delete failed; exiting non-zero.")
            sys.exit(1)

    else:
        logging.info("No command specified. Use --create, --delete, or --list.")


if __name__ == "__main__":
    app.run(main)
