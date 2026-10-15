"""Deployment script for Trend Trawler Agents."""

import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import dotenv
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
    # creative_agent post-render image QA (creative_agent/config.py): kill switch
    # (default on), per-image re-render budget (0..2, default 1), per-run
    # re-render cap (0..8, default 2) and the vision model.
    "IMAGE_QA_ENABLED": os.getenv("IMAGE_QA_ENABLED", "true"),
    "IMAGE_QA_MAX_RERENDERS": os.getenv("IMAGE_QA_MAX_RERENDERS", "1"),
    "IMAGE_QA_MAX_RERENDERS_PER_RUN": os.getenv("IMAGE_QA_MAX_RERENDERS_PER_RUN", "2"),
    "IMAGE_RENDER_CONCURRENCY": os.getenv("IMAGE_RENDER_CONCURRENCY", "2"),
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
    # trend_scout trend digest (trend_scout/digest.py): kill switch (default on;
    # off = only the sensitivity classifier runs) and the shared daily cache table.
    "TREND_DIGEST_ENABLED": os.getenv("TREND_DIGEST_ENABLED", "true"),
    "BQ_TABLE_TREND_DIGEST": os.getenv("BQ_TABLE_TREND_DIGEST") or "trend_digest",
    # creative_agent person casting (creative_agent/config.py): the style families
    # a cast concept may use and the cap on cast concepts per set (0..4, default
    # 2; 0 = casting off). Defaults from the calibration spikes: the photographic
    # group plus the illustrated families calibrated 2026-10-10.
    "PERSON_SAFE_STYLES": os.getenv("PERSON_SAFE_STYLES")
    or (
        "Candid 35mm film photo,Photoreal / editorial,Cinematic film still,"
        "3D character render,Comic panel,2D flat / vector cartoon,"
        "Collage / mixed-media"
    ),
    "MAX_CAST_CONCEPTS": os.getenv("MAX_CAST_CONCEPTS", "2"),
    # Timezone of every run's date state (agent_common/clock.py; the date itself
    # is computed per run at state init, never baked in at deploy time).
    "TREND_TIMEZONE": os.getenv("TREND_TIMEZONE") or "America/New_York",
    # creative_agent trend situation (creative_agent/trend_situation.py): kill
    # switch for the grounded "current moment" branch (default on), the event
    # window around today (1..30 days, default 7) and how long a just-happened
    # moment stays valid (6..168 hours, default 48).
    "TREND_STATE_ENABLED": os.getenv("TREND_STATE_ENABLED", "true"),
    "TREND_EVENT_WINDOW_DAYS": os.getenv("TREND_EVENT_WINDOW_DAYS", "7"),
    "TIMELY_VALID_HOURS_AFTER": os.getenv("TIMELY_VALID_HOURS_AFTER", "48"),
    # The timely slot: how many of the 4 creatives may reference a validated
    # current moment (0..2, default 1; 0 = off).
    "MAX_TIMELY_CONCEPTS": os.getenv("MAX_TIMELY_CONCEPTS", "1"),
    # Per-request timeout for flash / lite model calls (agent_common/genai_retry.py;
    # default 90, clamped 30..900, 0 = use MODEL_REQUEST_TIMEOUT_SECONDS). The ADK
    # agent models bake it in at deploy (pickled); the lazily built image-QA
    # client reads it in the engine runtime, hence shipped here.
    "FLASH_MODEL_REQUEST_TIMEOUT_SECONDS": os.getenv(
        "FLASH_MODEL_REQUEST_TIMEOUT_SECONDS", "90"
    ),
    # --- Knobs re-read in the engine runtime --------------------------------
    # Agent Engine cloudpickles module-level functions BY REFERENCE, so module
    # globals and class-level defaults are re-evaluated inside the engine from
    # the ENGINE's env, and lazily built configs/clients (image-gen, image-QA,
    # the creative_eval judge) read it at call time. These are shipped here or a
    # deploy.env change would never reach the engines. Defaults equal the
    # parsers' defaults (see each named module).
    # General per-request model timeout (agent_common/genai_retry.py; default
    # 240, clamped 30..900, 0 disables): the lazily built image-gen and judge
    # clients read the module global in the engine.
    "MODEL_REQUEST_TIMEOUT_SECONDS": os.getenv("MODEL_REQUEST_TIMEOUT_SECONDS", "240"),
    # Serving location pinned for gemini-3.x (agent_common/locations.py; default
    # `global`); also the judge's default location (creative_eval/config.py).
    "MODEL_LOCATION": os.getenv("MODEL_LOCATION", "global"),
    # Image-gen ImageConfig defaults (creative_agent/config.py class attributes,
    # read by creative_agent/image_tools.py at render time).
    "IMAGE_SIZE": os.getenv("IMAGE_SIZE", "2K"),
    "IMAGE_ASPECT_RATIO_DEFAULT": os.getenv("IMAGE_ASPECT_RATIO_DEFAULT", "9:16"),
    # creative_eval judge (creative_eval/config.py EvalConfig, built lazily in
    # the engine): model and concurrency caps. EVAL_MODEL_LOCATION is in
    # OPTIONAL_ENV_VARS (its default follows MODEL_LOCATION).
    "EVAL_MODEL": os.getenv("EVAL_MODEL", "gemini-3.1-pro-preview"),
    "EVAL_MAX_WORKERS": os.getenv("EVAL_MAX_WORKERS", "2"),
    "EVAL_MAX_VISUAL_WAVE_WORKERS": os.getenv("EVAL_MAX_VISUAL_WAVE_WORKERS", "4"),
    # Campaign-research model bucket (creative_agent/config.py; default
    # `global_altbucket`, unknown values fall back to it).
    "CAMPAIGN_RESEARCH_PLACEMENT": os.getenv(
        "CAMPAIGN_RESEARCH_PLACEMENT", "global_altbucket"
    ),
    # Sampling profile for the agents' explicit temperatures
    # (agent_common/sampling.py; default `tuned`, `default` = Gemini 3's 1.0,
    # unknown values fall back to `tuned`). Read at agent-module import, so the
    # pickled agents carry the deployer's value; shipped so the engine env
    # records which arm it is.
    "GEMINI_TEMPERATURE_PROFILE": os.getenv("GEMINI_TEMPERATURE_PROFILE", "tuned"),
}

# Shipped only when set in the deployer's env (never a None, never an invented
# value): CRITIC_FALLBACK_MODEL="" is a meaningful kill switch (no Pro -> flash
# failover; agent_common/config.py), so an unset var must stay unset and leave
# the code default (gemini-3.8-flash) in force. EVAL_MODEL_LOCATION's default is
# MODEL_LOCATION (shipped above), so it is only passed through when overridden.
OPTIONAL_ENV_VARS = ("CRITIC_FALLBACK_MODEL", "EVAL_MODEL_LOCATION")


def optional_env_vars() -> dict[str, str]:
    """The OPTIONAL_ENV_VARS set in the current env (an empty string counts)."""
    return {k: v for k in OPTIONAL_ENV_VARS if (v := os.getenv(k)) is not None}


ENV_VAR_DICT.update(optional_env_vars())


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
# Kept by hand; tests/test_deploy_import_graph.py imports each agent module and
# fails if it pulls in a repo-root package missing here. Current graph:
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
    "allow_protected",
    False,
    "with --delete: allow deleting the sessions engine or an engine .env still uses",
)
flags.DEFINE_bool(
    "enable_tracing",
    False,
    "with --create: export Cloud Trace spans (sets "
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true; no prompt/response content).",
)
flags.DEFINE_bool(
    "write_env",
    True,
    "with --create: write the new engine ID into the repo .env. Use --nowrite_env "
    "for CI / parallel deploys (Cloud Build), which hand IDs off via --output_json.",
)
flags.DEFINE_string(
    "output_json",
    None,
    "with --create: write the new engine as JSON {agent, resource_name, env_key, "
    "version, display_name, enable_tracing} to this path and print a "
    "machine-readable `ENGINE_ID agent=<agent> id=<resource name>` line.",
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


# Packaging: the SDK tars each extra_packages entry with a bare ``tarfile.add(path)``
# (no filter), so everything in a package dir would ship — a local ``adk web`` run's
# ``.adk/`` (session.db, rendered PNGs/PDFs, eval history: 100+ MB of user data),
# ``__pycache__`` and tool caches. deploy_agent stages a filtered copy instead.
# Hidden entries (``.adk``, ``.env``, ``.pytest_cache`` …) are always dropped; no
# agent package tracks a dotfile.
EXCLUDED_DIR_NAMES = frozenset({"__pycache__", "node_modules"})
EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo")

REQUIREMENTS_PATH = os.path.join(project_root, "requirements.txt")


def _is_local_only(name: str) -> bool:
    return (
        name.startswith(".")
        or name in EXCLUDED_DIR_NAMES
        or name.endswith(EXCLUDED_FILE_SUFFIXES)
    )


def _ignore_local_only(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if _is_local_only(n)}


def stage_extra_packages(
    packages: list[str], dest_root: str, src_root: str = project_root
) -> list[str]:
    """Copy each package dir into ``dest_root`` minus local-only files.

    Keeps each package's relative path (``./creative_agent`` →
    ``<dest_root>/creative_agent``) — the SDK's tar arcname is the path as given,
    and the flat layout needs it as the import path. Returns the same relative
    names, to be passed to the SDK with ``dest_root`` as the working directory.
    """
    for pkg in packages:
        rel = os.path.normpath(pkg)
        shutil.copytree(
            os.path.join(src_root, rel),
            os.path.join(dest_root, rel),
            ignore=_ignore_local_only,
        )
    return list(packages)


def assert_no_local_data(root: str) -> None:
    """Refuse to deploy if any staged path is local-only (belt and braces)."""
    bad = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        bad += [os.path.join(rel_dir, d) for d in dirnames if _is_local_only(d)]
        if any(_is_local_only(p) for p in parts):
            continue
        bad += [os.path.join(rel_dir, f) for f in filenames if _is_local_only(f)]
    if bad:
        raise RuntimeError(
            f"refusing to deploy: local-only paths staged into the engine package: "
            f"{sorted(bad)[:10]}"
        )


def load_requirements(path: str) -> list[str]:
    """Read a ``uv export`` requirements file as bare requirement lines.

    Drops the header, the ``# via`` annotations, inline comments and blank lines,
    which the SDK would otherwise try (and fail, ~600 warnings) to parse as
    constraints. requirements.txt itself stays the exact ``uv export`` output the
    CI drift check compares.
    """
    reqs = []
    with open(path) as f:
        for line in f:
            req = line.split(" #", 1)[0].strip()
            if req and not req.startswith("#"):
                reqs.append(req)
    return reqs


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
    except Exception as e:
        # Raise: a swallowed failure left the old ID in .env while the caller
        # reported `old -> new` and exited 0.
        logging.error(
            f"Could not write {KEY_NAME}={agent_engine_id} to {env_file_path}: {e}"
        )
        raise
    logging.info(f"Updated {KEY_NAME} in {env_file_path} to {agent_engine_id}")


def write_engine_record(
    path: str,
    name: str,
    version: str,
    resource_name: str,
    enable_tracing: bool,
) -> dict:
    """Write a deployed engine as JSON to ``path`` and print an ``ENGINE_ID`` line.

    The non-interactive hand-off for CI (deployment/cloudbuild.agents.yaml): each
    parallel deploy step writes its own file instead of racing on the shared .env,
    and the stdout line stays greppable in the build log even if a later step fails.
    """
    record = {
        "agent": name,
        "resource_name": resource_name,
        "env_key": engine_env_key(name),
        "version": version,
        "display_name": f"{AGENT_DEPLOY_SPECS[name]['display_name']}-{version}",
        "enable_tracing": enable_tracing,
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    print(f"ENGINE_ID agent={name} id={resource_name}", flush=True)
    return record


# ==============================
# CRUD ops
# ==============================
# TODO: add op for update()


# create deployment (unified): any agent in AGENT_DEPLOY_SPECS
def deploy_agent(
    name: str,
    version: str,
    enable_tracing: bool = False,
    *,
    write_env: bool = True,
    output_json: str | None = None,
) -> str:
    """Creates and deploys an Agent to Vertex AI Agent Engine Runtime.

    Parameterized by AGENT_DEPLOY_SPECS (module/prefix/naming) and
    AGENT_EXTRA_PACKAGES (bundled dirs) so all agents share one deploy path and
    a new agent only needs an entry in those two maps. ``enable_tracing`` opts the
    engine into Cloud Trace export (see ``build_env_vars``). ``write_env=False``
    leaves .env untouched and ``output_json`` writes a machine-readable record
    (``write_engine_record``) — the non-interactive mode Cloud Build uses. Returns
    the new engine's full resource name.
    """
    import importlib

    bucket = (os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET") or "").strip()
    if not bucket:
        # Fail before importing every agent (was a "gs://None" staging bucket).
        raise ValueError("GOOGLE_CLOUD_STORAGE_BUCKET must be set to deploy")

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

    requirements = load_requirements(REQUIREMENTS_PATH)

    try:
        logging.info(f"Deploying `{name}` agent...")
        with tempfile.TemporaryDirectory(prefix="engine-pkg-") as staging_root:
            staged = stage_extra_packages(extra_packages, staging_root)
            assert_no_local_data(staging_root)
            # The SDK tars the relative extra_packages paths from the cwd: run it
            # from the filtered staging copy, never the checkout.
            with contextlib.chdir(staging_root):
                remote_agent = _get_client().runtimes.create(
                    agent=adk_app,
                    config={
                        "requirements": requirements,
                        "extra_packages": staged,
                        "staging_bucket": f"gs://{bucket}",
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
        resource_name = remote_agent.api_resource.name
        if output_json:
            write_engine_record(
                output_json, name, version, resource_name, enable_tracing
            )
        if write_env:
            update_env_file(
                name=name,
                agent_engine_id=resource_name,
                env_file_path=ENV_FILE_PATH,
            )
    except Exception as e:
        logging.exception(f"Error deploying agent to Agent Engine Runtime: {e}")
        # Re-raise so main() can turn a failed deploy into a non-zero process exit
        # (a silent return here made `--create` failures exit 0 and mask CI/deploy
        # breakage).
        raise
    return resource_name


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


# Engines --delete refuses unless --allow_protected: the shared sessions store
# (force=True would delete every user's run history with it).
PROTECTED_DISPLAY_NAME_PREFIXES = ("trend-trawler-sessions",)


def protected_engine_reason(resource_id: str, display_name: object) -> str | None:
    """Why deleting ``resource_id`` needs --allow_protected, or None."""
    if isinstance(display_name, str) and display_name.startswith(
        PROTECTED_DISPLAY_NAME_PREFIXES
    ):
        return f"it is the sessions engine ({display_name})"
    for name in AGENT_NAMES:
        key = engine_env_key(name)
        value = (os.getenv(key) or "").strip().rstrip("/")
        if value and value.rsplit("/", 1)[-1] == resource_id:
            return f"{key} in the environment/.env still points at it"
    return None


def delete(resource_id: str, *, allow_protected: bool = False) -> None:
    """Deletes an existing agent engine (a bare ID or a full resource name).

    Looks the engine up first and refuses the sessions engine or an engine the
    current .env still uses unless ``allow_protected``.
    """
    resource_id = resource_id.strip().rstrip("/").rsplit("/", 1)[-1]
    logging.info(f"Attempting to delete agent: {resource_id}")

    PROJECT_NUM = os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER")
    if not PROJECT_NUM:
        raise ValueError("GOOGLE_CLOUD_PROJECT_NUMBER must be set to delete an engine")
    RESOURCE_NAME = f"projects/{PROJECT_NUM}/locations/{AGENT_ENGINE_LOCATION}/reasoningEngines/{resource_id}"

    client = _get_client()
    display_name = client.runtimes.get(name=RESOURCE_NAME).api_resource.display_name
    logging.info(f"Engine {resource_id} display name: {display_name}")
    reason = protected_engine_reason(resource_id, display_name)
    if reason and not allow_protected:
        raise ValueError(
            f"Refusing to delete {resource_id}: {reason}. "
            "Pass --allow_protected if this is really intended."
        )

    # runtimes.delete returns the long-running operation without polling it, so
    # report the request rather than claiming the engine is already gone.
    op = client.runtimes.delete(name=RESOURCE_NAME, force=True)
    if op.error:
        logging.warning(
            f"Delete operation {op.name} for {resource_id} error: {op.error}"
        )
    logging.info(f"Delete requested for {resource_id} (operation {op.name})")


def default_version() -> str:
    """The version namespace used when --version is omitted (UTC, to the minute)."""
    return datetime.now(UTC).strftime("%Y_%m_%d_%H_%M")


def main(argv):
    """Main function that uses the defined flags."""
    del argv

    if FLAGS.version is None:
        FLAGS.version = default_version()
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
                write_env=FLAGS.write_env,
                output_json=FLAGS.output_json,
            )
        except Exception as e:
            logging.error(f"Deploy failed ({e}); exiting non-zero.")
            sys.exit(1)

    elif FLAGS.delete:
        if not FLAGS.resource_id:
            logging.error("Error: --resource_id is required for the delete operation.")
            sys.exit(1)
        try:
            delete(
                resource_id=FLAGS.resource_id,
                allow_protected=FLAGS.allow_protected,
            )
        except Exception:
            logging.exception("Delete failed; exiting non-zero.")
            sys.exit(1)

    else:
        logging.info("No command specified. Use --create, --delete, or --list.")


if __name__ == "__main__":
    app.run(main)
