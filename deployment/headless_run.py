"""Headless end-to-end runner for the creative_agent (creative-eval branch).

Runs one full creative_agent invocation via a local ADK Runner, bypassing the
api_server SSE/HTTP request timeout that kills UI runs during the ~5-min eval
phase. Uses the same file artifact service directory (.adk/artifacts) the
api_server uses so save_artifact calls persist identically. Verifies:

  - each visual is rendered exactly once (visual_generator thinking_budget=0 fix)
  - creative_evaluation_report is produced and eval_report_gcs_uri is set
  - the HTML gallery is built
  - a BigQuery row is written

Run:  uv run python deployment/headless_run.py
"""

import os
import sys
import json
import asyncio
import logging
import warnings
from collections import Counter

import dotenv

# project root on path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

ENV_FILE_PATH = os.path.join(project_root, ".env")
dotenv.load_dotenv(dotenv_path=ENV_FILE_PATH)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")

from google.genai import types
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.artifacts.file_artifact_service import FileArtifactService

from creative_agent.agent import root_agent

APP_NAME = "creative_agent"
USER_ID = "headless_user"
ARTIFACT_DIR = os.path.join(project_root, ".adk", "artifacts")

KICKOFF = f"""Brand: {os.getenv("BRAND")}
Target Product: {os.getenv("TARGET_PRODUCT")}
Key Selling Point(s): {os.getenv("KEY_SELLING_POINT")}
Target Audience: {os.getenv("TARGET_AUDIENCE")}
Target Search Trend: {os.getenv("TARGET_SEARCH_TREND")}
"""

# tool-call tallies keyed by tool name
tool_calls: Counter = Counter()
# last response payload per tool name (for the terminal persistence tools)
tool_responses: dict = {}


def _log_event(event):
    author = getattr(event, "author", "?")
    content = getattr(event, "content", None)
    if not content or not getattr(content, "parts", None):
        return
    for part in content.parts:
        fc = getattr(part, "function_call", None)
        fr = getattr(part, "function_response", None)
        txt = getattr(part, "text", None)
        if fc:
            tool_calls[fc.name] += 1
            logging.info(f"[{author}] -> call: {fc.name}")
        elif fr:
            resp = fr.response
            tool_responses[fr.name] = resp
            status = resp.get("status") if isinstance(resp, dict) else None
            logging.info(f"[{author}] <- resp: {fr.name} (status={status})")
        elif txt and txt.strip():
            snippet = txt.strip().replace("\n", " ")[:160]
            logging.info(f"[{author}]: {snippet}")


async def main():
    session_service = InMemorySessionService()
    artifact_service = FileArtifactService(root_dir=ARTIFACT_DIR)

    session = await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, state={}
    )
    logging.info(f"session id: {session.id}")

    runner = Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=session_service,
        artifact_service=artifact_service,
    )

    msg = types.Content(role="user", parts=[types.Part(text=KICKOFF)])
    async for event in runner.run_async(
        user_id=USER_ID, session_id=session.id, new_message=msg
    ):
        _log_event(event)

    # ---- final state inspection ----
    final = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session.id
    )
    state = dict(final.state)

    print("\n" + "=" * 70)
    print("TOOL CALL COUNTS")
    print("=" * 70)
    for name, n in sorted(tool_calls.items()):
        print(f"  {name}: {n}")

    print("\n" + "=" * 70)
    print("KEY STATE VALUES")
    print("=" * 70)
    for k in [
        "gcs_bucket",
        "gcs_folder",
        "agent_output_dir",
        "_images_generated",
        "_generated_artifact_keys",
        "eval_report_gcs_uri",
        "creative_gallery_gcs_uri",
    ]:
        v = state.get(k)
        if isinstance(v, (dict, list)):
            v = json.dumps(v)[:200]
        print(f"  {k}: {v}")

    report = state.get("creative_evaluation_report")
    print(f"\n  creative_evaluation_report present: {report is not None}")
    if isinstance(report, dict):
        print(f"  report keys: {list(report.keys())}")

    print("\n" + "=" * 70)
    print("VALIDATION")
    print("=" * 70)
    gen_calls = tool_calls.get("generate_image", 0)
    artifact_keys = state.get("_generated_artifact_keys") or []
    if isinstance(artifact_keys, dict):
        artifact_keys = list(artifact_keys.values())

    def _ok(name):
        r = tool_responses.get(name)
        return isinstance(r, dict) and r.get("status") in ("success", "ok")

    gallery = tool_responses.get("save_creative_gallery_html") or {}
    bq = tool_responses.get("write_trends_to_bq") or {}
    print(f"  generate_image tool calls: {gen_calls} (expected exactly 1)")
    print(f"  generated artifact keys: {len(artifact_keys)}")
    print(
        f"  eval report saved: {_ok('save_eval_report_to_gcs')} "
        f"uri={state.get('eval_report_gcs_uri')}"
    )
    print(
        f"  gallery built: {_ok('save_creative_gallery_html')} "
        f"uri={gallery.get('gcs_uri')}"
    )
    print(f"  bq row written: {_ok('write_trends_to_bq')} resp={json.dumps(bq)[:160]}")
    print(f"\nsession id for reference: {session.id}")
    print(f"gcs folder: {state.get('gcs_folder')}")


if __name__ == "__main__":
    asyncio.run(main())
