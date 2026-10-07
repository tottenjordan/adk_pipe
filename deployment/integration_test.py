"""Integration tests for deployed Agent Engine instances.

Runs against live GCP infrastructure. Requires:
  - Authenticated GCP credentials (gcloud auth application-default login)
  - .env file with SCOUT_/CREATIVE_/INTERACTIVE_AGENT_ENGINE_ID populated
  - Deployed agents on Agent Engine

Usage:
  # Health check — verify agents are reachable
  python deployment/integration_test.py --check health

  # Session lifecycle — create, verify, delete sessions
  python deployment/integration_test.py --check session --agent trend_scout

  # Smoke test — run agent end-to-end, assert session state keys (creative_agent:
  # also finalize_done + the saved eval report / research PDF URIs)
  python deployment/integration_test.py --check smoke --agent creative_agent

  # Run all checks for all agents
  python deployment/integration_test.py --check all
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass

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
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

ENV_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
dotenv.load_dotenv(dotenv_path=ENV_FILE_PATH)

# Derived from deploy_agent's spec map so every deployable agent is testable and
# the env key always matches what `deploy_agent.py --create` writes to .env.
AGENT_ENV_KEYS = {name: engine_env_key(name) for name in AGENT_DEPLOY_SPECS}

# Session state keys that should be populated after a successful agent run
EXPECTED_STATE_KEYS = {
    "trend_scout": [
        "brand",
        "target_product",
        "target_audience",
        "key_selling_points",
    ],
    "creative_agent": [
        "brand",
        "target_product",
        "target_audience",
        "key_selling_points",
        "target_search_trends",
    ],
    # Runs until the first human-review checkpoint (review_research) pauses it;
    # the campaign metadata is memorized before that.
    "interactive_creative": [
        "brand",
        "target_product",
        "target_audience",
        "key_selling_points",
        "target_search_trends",
    ],
}

# Deliverables a COMPLETE creative_agent run leaves in session state: the
# campaign keys above survive a run that ends early (e.g. the root's empty turns
# before finalize, 2026-10-07), so the smoke check also requires that finalize
# ran (`finalize_done`) and that the eval report + research PDF were saved.
CREATIVE_OUTPUT_KEYS = (
    "finalize_done",
    "eval_report_gcs_uri",
    "research_report_gcs_uri",
)

TEST_USER_ID = "integration_test_user"

# interactive_creative's LongRunningFunctionTool checkpoints: a smoke run pauses at
# the first one instead of emitting final text.
INTERACTIVE_CHECKPOINT_TOOLS = frozenset(
    {"review_research", "review_ad_copies", "review_visual_concepts"}
)


# ==============================
# Response/event parsing helpers
# ==============================
def _get(obj, key: str):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _session_ids(resp) -> list[str]:
    """Session ids from list_sessions: a `{'sessions': [...]}` wrapper (2.x) or a list."""
    if resp is None:
        return []
    if isinstance(resp, dict) or hasattr(resp, "sessions"):
        resp = _get(resp, "sessions") or []
    return [sid for s in resp if (sid := _get(s, "id")) is not None]


def _event_parts(event) -> list:
    if not isinstance(event, dict):
        return []
    return (event.get("content") or {}).get("parts") or []


def _function_call_names(event) -> list[str]:
    names = []
    for part in _event_parts(event):
        call = part.get("function_call") or part.get("functionCall")
        if call:
            names.append(call.get("name", "?"))
    return names


def _event_texts(event) -> list[str]:
    return [t for part in _event_parts(event) if (t := part.get("text")) and t.strip()]


def _check_text_output(agent_name: str, events: list) -> "TestResult":
    name = f"smoke:{agent_name}:has_text_output"
    texts = [t for e in events for t in _event_texts(e)]
    if texts:
        return TestResult(
            name=name, passed=True, message=f"{len(texts)} text responses"
        )
    if agent_name == "interactive_creative":
        paused = [
            n
            for e in events
            for n in _function_call_names(e)
            if n in INTERACTIVE_CHECKPOINT_TOOLS
        ]
        if paused:
            return TestResult(
                name=name, passed=True, message=f"Paused at checkpoint: {paused[0]}"
            )
    return TestResult(name=name, passed=False, message="No text output from agent")


def _is_set(value) -> bool:
    return bool(value.strip()) if isinstance(value, str) else bool(value)


def check_creative_outputs(agent_name: str, state: dict) -> "TestResult | None":
    """Assert a creative_agent run's real outputs (None for other agents).

    interactive_creative's smoke run pauses at checkpoint 1, long before
    finalize, and trend_scout has no creative outputs, so only creative_agent is
    checked. A failure names each missing key and any `<key>__issues` the
    fail-soft step recorded.
    """
    if agent_name != "creative_agent":
        return None
    name = f"smoke:{agent_name}:outputs"
    missing = [k for k in CREATIVE_OUTPUT_KEYS if not _is_set(state.get(k))]
    if not missing:
        return TestResult(
            name=name,
            passed=True,
            message=(
                f"finalize done; eval report {state['eval_report_gcs_uri']}; "
                f"research PDF {state['research_report_gcs_uri']}"
            ),
        )
    details = [
        f"{k} (issue: {issue})" if (issue := state.get(f"{k}__issues")) else k
        for k in missing
    ]
    return TestResult(
        name=name,
        passed=False,
        message=(
            "Run did not produce its outputs — missing/empty: "
            + ", ".join(details)
            + " (did the run end before finalize_pipeline?)"
        ),
    )


# ==============================
# Result tracking
# ==============================
@dataclass
class TestResult:
    name: str
    passed: bool
    message: str
    duration_s: float = 0.0
    # Not run (e.g. agent not deployed: engine-ID env var unset). Reported as SKIP
    # and excluded from failures / the exit code.
    skipped: bool = False

    @property
    def failed(self) -> bool:
        return not self.passed and not self.skipped


class EngineIdNotSetError(ValueError):
    """The agent's engine-ID env var is unset — i.e. it isn't deployed (skip)."""


def print_results(results: list[TestResult]) -> bool:
    """Print test results and return True if nothing failed (skips are OK)."""
    print("\n" + "=" * 60)
    print("INTEGRATION TEST RESULTS")
    print("=" * 60)

    all_passed = True
    for r in results:
        status = "SKIP" if r.skipped else "PASS" if r.passed else "FAIL"
        duration = f" ({r.duration_s:.1f}s)" if r.duration_s > 0 else ""
        print(f"  [{status}] {r.name}{duration}")
        if not r.passed:
            print(f"         {r.message}")
        if r.failed:
            all_passed = False

    print("=" * 60)
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if r.failed)
    skipped = sum(1 for r in results if r.skipped)
    print(
        f"  {passed} passed, {failed} failed, {skipped} skipped, {len(results)} total"
    )
    print("=" * 60 + "\n")
    return all_passed


# ==============================
# Agent Runtime (agentplatform) client
# ==============================
def get_client():
    # Agent Engine is a *regional* resource, so it uses GCP_REGION (us-central1) —
    # NOT GOOGLE_CLOUD_LOCATION, which is set to `global` for the gemini-3.x models.
    return agentplatform.Client(
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=os.getenv("GCP_REGION", "us-central1"),
    )  # pyright: ignore[reportCallIssue]


def get_remote_agent(client, agent_name: str):
    """Get a remote agent handle by name."""
    env_key = AGENT_ENV_KEYS[agent_name]
    resource_id = os.getenv(env_key)
    if not resource_id:
        raise EngineIdNotSetError(
            f"{env_key} not set in .env — agent not deployed, skip"
        )
    return client.runtimes.get(name=resource_id)


# ==============================
# Check: Health
# ==============================
def check_health(client) -> list[TestResult]:
    """Verify deployed Agent Engine instances are reachable."""
    results = []

    for agent_name, env_key in AGENT_ENV_KEYS.items():
        resource_id = os.getenv(env_key)
        if not resource_id:
            results.append(
                TestResult(
                    name=f"health:{agent_name}",
                    passed=False,
                    message=f"{env_key} not set in .env — skip",
                    skipped=True,
                )
            )
            continue

        start = time.time()
        try:
            remote_agent = client.runtimes.get(name=resource_id)

            # Verify basic properties are populated
            api_resource = remote_agent.api_resource
            checks = []
            if not api_resource.name:
                checks.append("name is empty")
            if not api_resource.display_name:
                checks.append("display_name is empty")

            if checks:
                results.append(
                    TestResult(
                        name=f"health:{agent_name}",
                        passed=False,
                        message=f"Agent reachable but: {', '.join(checks)}",
                        duration_s=time.time() - start,
                    )
                )
            else:
                results.append(
                    TestResult(
                        name=f"health:{agent_name}",
                        passed=True,
                        message=f"OK — {api_resource.display_name}",
                        duration_s=time.time() - start,
                    )
                )
                logging.info(
                    f"  {agent_name}: name={api_resource.name}, "
                    f"display_name={api_resource.display_name}, "
                    f"create_time={api_resource.create_time}"
                )

        except Exception as e:
            results.append(
                TestResult(
                    name=f"health:{agent_name}",
                    passed=False,
                    message=f"{type(e).__name__}: {e}",
                    duration_s=time.time() - start,
                )
            )

    return results


# ==============================
# Check: Session lifecycle
# ==============================
async def check_session(client, agent_name: str) -> list[TestResult]:
    """Test session create → list → delete lifecycle."""
    results = []
    session = None

    try:
        remote_agent = get_remote_agent(client, agent_name)
    except EngineIdNotSetError as e:
        return [
            TestResult(
                name=f"session:{agent_name}:get_agent",
                passed=False,
                message=str(e),
                skipped=True,
            )
        ]

    # 1. Create session
    start = time.time()
    try:
        session = await remote_agent.async_create_session(user_id=TEST_USER_ID)
        has_id = (
            "id" in session if isinstance(session, dict) else hasattr(session, "id")
        )
        results.append(
            TestResult(
                name=f"session:{agent_name}:create",
                passed=has_id,
                message="Session created"
                if has_id
                else f"No 'id' in session: {session}",
                duration_s=time.time() - start,
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                name=f"session:{agent_name}:create",
                passed=False,
                message=f"{type(e).__name__}: {e}",
                duration_s=time.time() - start,
            )
        )
        return results  # can't continue without a session

    session_id = session["id"] if isinstance(session, dict) else session.id

    # 2. List sessions — verify ours exists
    start = time.time()
    try:
        session_ids = _session_ids(
            await remote_agent.async_list_sessions(user_id=TEST_USER_ID)
        )

        found = session_id in session_ids
        results.append(
            TestResult(
                name=f"session:{agent_name}:list",
                passed=found,
                message="Session found in list"
                if found
                else f"Session {session_id} not in {session_ids}",
                duration_s=time.time() - start,
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                name=f"session:{agent_name}:list",
                passed=False,
                message=f"{type(e).__name__}: {e}",
                duration_s=time.time() - start,
            )
        )

    # 3. Delete session
    start = time.time()
    try:
        await remote_agent.async_delete_session(
            user_id=TEST_USER_ID, session_id=session_id
        )
        results.append(
            TestResult(
                name=f"session:{agent_name}:delete",
                passed=True,
                message="Session deleted",
                duration_s=time.time() - start,
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                name=f"session:{agent_name}:delete",
                passed=False,
                message=f"{type(e).__name__}: {e}",
                duration_s=time.time() - start,
            )
        )

    # 4. Verify session is gone
    start = time.time()
    try:
        session_ids_after = _session_ids(
            await remote_agent.async_list_sessions(user_id=TEST_USER_ID)
        )

        gone = session_id not in session_ids_after
        results.append(
            TestResult(
                name=f"session:{agent_name}:verify_deleted",
                passed=gone,
                message="Session confirmed deleted"
                if gone
                else "Session still exists after delete",
                duration_s=time.time() - start,
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                name=f"session:{agent_name}:verify_deleted",
                passed=False,
                message=f"{type(e).__name__}: {e}",
                duration_s=time.time() - start,
            )
        )

    return results


# ==============================
# Check: Smoke test
# ==============================
async def check_smoke(client, agent_name: str) -> list[TestResult]:
    """Run agent end-to-end and assert session state contains expected keys."""
    results = []

    try:
        remote_agent = get_remote_agent(client, agent_name)
    except EngineIdNotSetError as e:
        return [
            TestResult(
                name=f"smoke:{agent_name}:get_agent",
                passed=False,
                message=str(e),
                skipped=True,
            )
        ]

    # Build test query from .env
    test_query = (
        f"Brand: {os.getenv('BRAND', 'Test Brand')}\n"
        f"Target Product: {os.getenv('TARGET_PRODUCT', 'Test Product')}\n"
        f"Key Selling Point(s): {os.getenv('KEY_SELLING_POINT', 'Test selling point')}\n"
        f"Target Audience: {os.getenv('TARGET_AUDIENCE', 'Test audience')}\n"
        f"Target Search Trend: {os.getenv('TARGET_SEARCH_TREND', 'test trend')}\n"
    )

    session = None

    # 1. Create session
    start = time.time()
    try:
        session = await remote_agent.async_create_session(user_id=TEST_USER_ID)
        results.append(
            TestResult(
                name=f"smoke:{agent_name}:create_session",
                passed=True,
                message="Session created",
                duration_s=time.time() - start,
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                name=f"smoke:{agent_name}:create_session",
                passed=False,
                message=f"{type(e).__name__}: {e}",
                duration_s=time.time() - start,
            )
        )
        return results

    session_id = session["id"] if isinstance(session, dict) else session.id

    # 2. Run agent (stream query)
    start = time.time()
    events = []
    try:
        async for event in remote_agent.async_stream_query(
            user_id=TEST_USER_ID,
            session_id=session_id,
            message=test_query,
        ):
            events.append(event)
            # Log progress markers
            author = (
                event.get("author", "unknown") if isinstance(event, dict) else "unknown"
            )
            for tool_name in _function_call_names(event):
                logging.info(f"  [{author}] tool: {tool_name}")

        results.append(
            TestResult(
                name=f"smoke:{agent_name}:run",
                passed=len(events) > 0,
                message=f"Received {len(events)} events"
                if events
                else "No events received",
                duration_s=time.time() - start,
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                name=f"smoke:{agent_name}:run",
                passed=False,
                message=f"{type(e).__name__}: {e}",
                duration_s=time.time() - start,
            )
        )

    # 3. Verify session state contains expected keys
    start = time.time()
    try:
        session_data = await remote_agent.async_get_session(
            user_id=TEST_USER_ID, session_id=session_id
        )

        # Extract state — may be dict or object
        state = {}
        if isinstance(session_data, dict):
            state = session_data.get("state", {}) or {}
        elif hasattr(session_data, "state"):
            state = session_data.state or {}

        expected_keys = EXPECTED_STATE_KEYS.get(agent_name, [])
        missing_keys = [k for k in expected_keys if k not in state]

        if (outputs := check_creative_outputs(agent_name, state)) is not None:
            results.append(outputs)

        if missing_keys:
            results.append(
                TestResult(
                    name=f"smoke:{agent_name}:state_keys",
                    passed=False,
                    message=f"Missing state keys: {missing_keys}. Present: {list(state.keys())}",
                    duration_s=time.time() - start,
                )
            )
        else:
            results.append(
                TestResult(
                    name=f"smoke:{agent_name}:state_keys",
                    passed=True,
                    message=f"All expected keys present: {expected_keys}",
                    duration_s=time.time() - start,
                )
            )

    except Exception as e:
        results.append(
            TestResult(
                name=f"smoke:{agent_name}:state_keys",
                passed=False,
                message=f"{type(e).__name__}: {e}",
                duration_s=time.time() - start,
            )
        )

    # 4. Check that at least one text response was generated (or, for
    # interactive_creative, that the run paused at a review checkpoint)
    results.append(_check_text_output(agent_name, events))

    # 5. Cleanup — delete session
    try:
        await remote_agent.async_delete_session(
            user_id=TEST_USER_ID, session_id=session_id
        )
        logging.info(f"  Cleaned up session {session_id}")
    except Exception as e:
        logging.warning(f"  Failed to clean up session: {e}")

    return results


# ==============================
# Main
# ==============================
async def run_checks(check_type: str, agent_name: str | None) -> bool:
    """Run the requested checks and return True if all passed."""
    client = get_client()
    all_results: list[TestResult] = []

    agents_to_test = [agent_name] if agent_name else list(AGENT_DEPLOY_SPECS)

    if check_type in ("health", "all"):
        logging.info("Running health checks...")
        all_results.extend(check_health(client))

    if check_type in ("session", "all"):
        for agent in agents_to_test:
            logging.info(f"Running session lifecycle check for {agent}...")
            all_results.extend(await check_session(client, agent))

    if check_type in ("smoke", "all"):
        for agent in agents_to_test:
            logging.info(f"Running smoke test for {agent}...")
            logging.info("  (this may take several minutes)")
            all_results.extend(await check_smoke(client, agent))

    return print_results(all_results)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Integration tests for deployed Agent Engine instances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python deployment/integration_test.py --check health
  python deployment/integration_test.py --check session --agent trend_scout
  python deployment/integration_test.py --check smoke --agent creative_agent
  python deployment/integration_test.py --check all
        """,
    )
    parser.add_argument(
        "--check",
        choices=["health", "session", "smoke", "all"],
        required=True,
        help="Which check to run",
    )
    parser.add_argument(
        "--agent",
        choices=list(AGENT_DEPLOY_SPECS),
        default=None,
        help="Agent to test (default: all). Required for session and smoke checks.",
    )
    return parser


def main():
    args = build_parser().parse_args()

    all_passed = asyncio.run(run_checks(args.check, args.agent))
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
