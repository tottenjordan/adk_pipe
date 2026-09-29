"""Guard the SDK surface the repo depends on (P1b).

The root env takes google-cloud-aiplatform 2.x through a `[tool.uv]
override-dependencies` entry (google-adk's `eval` extra caps it <2 as policy).
aiplatform 2.x ships both `agentplatform` (the API our deploy scripts use) and
`vertexai` (the deprecated compat API ADK's VertexAiSessionService still
imports), so both surfaces must be present.
"""

import importlib.metadata

import agentplatform
import vertexai


def test_aiplatform_is_2x():
    major = int(importlib.metadata.version("google-cloud-aiplatform").split(".")[0])
    assert major == 2


def test_agentplatform_client_exposes_runtimes_and_sessions():
    client = agentplatform.Client(project="p", location="us-central1")
    assert hasattr(client, "runtimes")
    assert hasattr(client, "sessions")


def test_agentplatform_frameworks_adkapp_importable():
    from agentplatform.frameworks import AdkApp  # noqa: F401


def test_vertexai_compat_still_present_for_adk_session_service():
    # google/adk/sessions/vertex_ai_session_service.py calls
    # vertexai.Client(...).aio.agent_engines.sessions.*; drop this test once ADK
    # moves to agentplatform (then the override can go too).
    client = vertexai.Client(project="p", location="us-central1")
    assert hasattr(client.agent_engines, "sessions")
    assert hasattr(client.aio.agent_engines, "sessions")
    assert hasattr(client.aio.agent_engines.sessions, "events")
