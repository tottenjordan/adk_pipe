"""Integration tests for deployed Agent Engine instances.

Runs against live GCP infrastructure. Requires:
  - Authenticated GCP credentials (gcloud auth application-default login)
  - .env file with TRAWLER_AGENT_ENGINE_ID and/or CREATIVE_AGENT_ENGINE_ID populated
  - Deployed agents on Agent Engine

Usage:
  # Health check — verify agents are reachable
  python deployment/integration_test.py --check health

  # Session lifecycle — create, verify, delete sessions
  python deployment/integration_test.py --check session --agent trend_trawler

  # Smoke test — run agent end-to-end, assert session state keys
  python deployment/integration_test.py --check smoke --agent creative_agent

  # Run all checks for both agents
  python deployment/integration_test.py --check all
"""

import os
import sys
import json
import dotenv
import asyncio
import logging
import argparse
import time
import warnings
from dataclasses import dataclass

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

ENV_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
dotenv.load_dotenv(dotenv_path=ENV_FILE_PATH)

AGENT_ENV_KEYS = {
    "trend_trawler": "TRAWLER_AGENT_ENGINE_ID",
    "creative_agent": "CREATIVE_AGENT_ENGINE_ID",
}

# Session state keys that should be populated after a successful agent run
EXPECTED_STATE_KEYS = {
    "trend_trawler": ["brand", "target_product", "target_audience", "key_selling_points"],
    "creative_agent": [
        "brand", "target_product", "target_audience",
        "key_selling_points", "target_search_trends",
    ],
}

TEST_USER_ID = "integration_test_user"


# ==============================
# Result tracking
# ==============================
@dataclass
class TestResult:
    name: str
    passed: bool
    message: str
    duration_s: float = 0.0


def print_results(results: list[TestResult]) -> bool:
    """Print test results and return True if all passed."""
    print("\n" + "=" * 60)
    print("INTEGRATION TEST RESULTS")
    print("=" * 60)

    all_passed = True
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        duration = f" ({r.duration_s:.1f}s)" if r.duration_s > 0 else ""
        print(f"  [{status}] {r.name}{duration}")
        if not r.passed:
            print(f"         {r.message}")
            all_passed = False

    print("=" * 60)
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    print(f"  {passed} passed, {failed} failed, {len(results)} total")
    print("=" * 60 + "\n")
    return all_passed


# ==============================
# Vertex AI client
# ==============================
def get_client():
    return vertexai.Client(
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=os.getenv("GOOGLE_CLOUD_LOCATION"),
    )  # pyright: ignore[reportCallIssue]


def get_remote_agent(client, agent_name: str):
    """Get a remote agent handle by name."""
    env_key = AGENT_ENV_KEYS[agent_name]
    resource_id = os.getenv(env_key)
    if not resource_id:
        raise ValueError(
            f"{env_key} is not set in .env — deploy the agent first"
        )
    return client.agent_engines.get(name=resource_id)


# ==============================
# Check: Health
# ==============================
def check_health(client) -> list[TestResult]:
    """Verify deployed Agent Engine instances are reachable."""
    results = []

    for agent_name, env_key in AGENT_ENV_KEYS.items():
        resource_id = os.getenv(env_key)
        if not resource_id:
            results.append(TestResult(
                name=f"health:{agent_name}",
                passed=False,
                message=f"{env_key} not set in .env — skip",
            ))
            continue

        start = time.time()
        try:
            remote_agent = client.agent_engines.get(name=resource_id)

            # Verify basic properties are populated
            api_resource = remote_agent.api_resource
            checks = []
            if not api_resource.name:
                checks.append("name is empty")
            if not api_resource.display_name:
                checks.append("display_name is empty")

            if checks:
                results.append(TestResult(
                    name=f"health:{agent_name}",
                    passed=False,
                    message=f"Agent reachable but: {', '.join(checks)}",
                    duration_s=time.time() - start,
                ))
            else:
                results.append(TestResult(
                    name=f"health:{agent_name}",
                    passed=True,
                    message=f"OK — {api_resource.display_name}",
                    duration_s=time.time() - start,
                ))
                logging.info(
                    f"  {agent_name}: name={api_resource.name}, "
                    f"display_name={api_resource.display_name}, "
                    f"create_time={api_resource.create_time}"
                )

        except Exception as e:
            results.append(TestResult(
                name=f"health:{agent_name}",
                passed=False,
                message=f"{type(e).__name__}: {e}",
                duration_s=time.time() - start,
            ))

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
    except ValueError as e:
        return [TestResult(
            name=f"session:{agent_name}:get_agent",
            passed=False,
            message=str(e),
        )]

    # 1. Create session
    start = time.time()
    try:
        session = await remote_agent.async_create_session(user_id=TEST_USER_ID)
        has_id = "id" in session if isinstance(session, dict) else hasattr(session, "id")
        results.append(TestResult(
            name=f"session:{agent_name}:create",
            passed=has_id,
            message="Session created" if has_id else f"No 'id' in session: {session}",
            duration_s=time.time() - start,
        ))
    except Exception as e:
        results.append(TestResult(
            name=f"session:{agent_name}:create",
            passed=False,
            message=f"{type(e).__name__}: {e}",
            duration_s=time.time() - start,
        ))
        return results  # can't continue without a session

    session_id = session["id"] if isinstance(session, dict) else session.id

    # 2. List sessions — verify ours exists
    start = time.time()
    try:
        sessions = await remote_agent.async_list_sessions(user_id=TEST_USER_ID)
        # sessions may be a list of dicts or objects
        session_ids = []
        for s in sessions:
            sid = s["id"] if isinstance(s, dict) else s.id
            session_ids.append(sid)

        found = session_id in session_ids
        results.append(TestResult(
            name=f"session:{agent_name}:list",
            passed=found,
            message="Session found in list" if found else f"Session {session_id} not in {session_ids}",
            duration_s=time.time() - start,
        ))
    except Exception as e:
        results.append(TestResult(
            name=f"session:{agent_name}:list",
            passed=False,
            message=f"{type(e).__name__}: {e}",
            duration_s=time.time() - start,
        ))

    # 3. Delete session
    start = time.time()
    try:
        await remote_agent.async_delete_session(
            user_id=TEST_USER_ID, session_id=session_id
        )
        results.append(TestResult(
            name=f"session:{agent_name}:delete",
            passed=True,
            message="Session deleted",
            duration_s=time.time() - start,
        ))
    except Exception as e:
        results.append(TestResult(
            name=f"session:{agent_name}:delete",
            passed=False,
            message=f"{type(e).__name__}: {e}",
            duration_s=time.time() - start,
        ))

    # 4. Verify session is gone
    start = time.time()
    try:
        sessions_after = await remote_agent.async_list_sessions(user_id=TEST_USER_ID)
        session_ids_after = []
        for s in sessions_after:
            sid = s["id"] if isinstance(s, dict) else s.id
            session_ids_after.append(sid)

        gone = session_id not in session_ids_after
        results.append(TestResult(
            name=f"session:{agent_name}:verify_deleted",
            passed=gone,
            message="Session confirmed deleted" if gone else "Session still exists after delete",
            duration_s=time.time() - start,
        ))
    except Exception as e:
        results.append(TestResult(
            name=f"session:{agent_name}:verify_deleted",
            passed=False,
            message=f"{type(e).__name__}: {e}",
            duration_s=time.time() - start,
        ))

    return results


# ==============================
# Check: Smoke test
# ==============================
async def check_smoke(client, agent_name: str) -> list[TestResult]:
    """Run agent end-to-end and assert session state contains expected keys."""
    results = []

    try:
        remote_agent = get_remote_agent(client, agent_name)
    except ValueError as e:
        return [TestResult(
            name=f"smoke:{agent_name}:get_agent",
            passed=False,
            message=str(e),
        )]

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
        results.append(TestResult(
            name=f"smoke:{agent_name}:create_session",
            passed=True,
            message="Session created",
            duration_s=time.time() - start,
        ))
    except Exception as e:
        results.append(TestResult(
            name=f"smoke:{agent_name}:create_session",
            passed=False,
            message=f"{type(e).__name__}: {e}",
            duration_s=time.time() - start,
        ))
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
            author = event.get("author", "unknown") if isinstance(event, dict) else "unknown"
            if isinstance(event, dict) and "content" in event:
                parts = event["content"].get("parts", [])
                for part in parts:
                    if "functionCall" in part:
                        logging.info(f"  [{author}] tool: {part['functionCall'].get('name', '?')}")

        results.append(TestResult(
            name=f"smoke:{agent_name}:run",
            passed=len(events) > 0,
            message=f"Received {len(events)} events" if events else "No events received",
            duration_s=time.time() - start,
        ))
    except Exception as e:
        results.append(TestResult(
            name=f"smoke:{agent_name}:run",
            passed=False,
            message=f"{type(e).__name__}: {e}",
            duration_s=time.time() - start,
        ))

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

        if missing_keys:
            results.append(TestResult(
                name=f"smoke:{agent_name}:state_keys",
                passed=False,
                message=f"Missing state keys: {missing_keys}. Present: {list(state.keys())}",
                duration_s=time.time() - start,
            ))
        else:
            results.append(TestResult(
                name=f"smoke:{agent_name}:state_keys",
                passed=True,
                message=f"All expected keys present: {expected_keys}",
                duration_s=time.time() - start,
            ))

    except Exception as e:
        results.append(TestResult(
            name=f"smoke:{agent_name}:state_keys",
            passed=False,
            message=f"{type(e).__name__}: {e}",
            duration_s=time.time() - start,
        ))

    # 4. Check that at least one text response was generated
    text_events = []
    for event in events:
        if isinstance(event, dict) and "content" in event:
            for part in event["content"].get("parts", []):
                if "text" in part and part["text"].strip():
                    text_events.append(part["text"][:80])

    results.append(TestResult(
        name=f"smoke:{agent_name}:has_text_output",
        passed=len(text_events) > 0,
        message=f"{len(text_events)} text responses" if text_events else "No text output from agent",
    ))

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

    agents_to_test = (
        [agent_name] if agent_name
        else ["trend_trawler", "creative_agent"]
    )

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


def main():
    parser = argparse.ArgumentParser(
        description="Integration tests for deployed Agent Engine instances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python deployment/integration_test.py --check health
  python deployment/integration_test.py --check session --agent trend_trawler
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
        choices=["trend_trawler", "creative_agent"],
        default=None,
        help="Agent to test (default: both). Required for session and smoke checks.",
    )
    args = parser.parse_args()

    all_passed = asyncio.run(run_checks(args.check, args.agent))
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
