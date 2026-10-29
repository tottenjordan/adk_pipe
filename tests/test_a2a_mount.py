"""Tests for runserver.a2a_mount (offline, no GCP credentials)."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.types import a2a_pb2
from fastapi import FastAPI
from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event, EventActions
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from runserver.a2a_mount import (
    A2A_SERVICE_USER,
    DEFAULT_A2A_AGENTS,
    BoundedInMemoryTaskStore,
    ContextSessionMap,
    a2a_enabled,
    agent_card_dict,
    build_agent_card,
    build_studio_url,
    mint_handoff_token,
    mount_a2a_apps,
    verify_handoff_token,
)


class _VertexStyleSessionService(InMemorySessionService):
    """Mimics VertexAiSessionService: rejects client-supplied session_id on create."""

    def __init__(self) -> None:
        super().__init__()
        self.created_calls: list[dict[str, Any]] = []
        self._counter = 0

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ):
        self.created_calls.append(
            {
                "app_name": app_name,
                "user_id": user_id,
                "state": dict(state or {}),
                "session_id": session_id,
            }
        )
        if session_id is not None:
            raise ValueError(
                "400 INVALID_ARGUMENT: User-provided session_id is not supported by VertexAiSessionService"
            )
        self._counter += 1
        minted_id = f"vertex-sid-{self._counter}"
        return await super().create_session(
            app_name=app_name,
            user_id=user_id,
            state=state,
            session_id=minted_id,
        )


class _RecordingPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="recording_test_plugin")
        self.run_count = 0

    async def before_run_callback(self, *, invocation_context: InvocationContext):
        self.run_count += 1
        return None


class _StubEchoAgent(BaseAgent):
    """Deterministic BaseAgent that echoes text or emits a long-running tool call."""

    pause_on_keyword: str = "PAUSE_CHECKPOINT"

    async def _run_async_impl(self, ctx: InvocationContext):
        user_text = ""
        if ctx.user_content and ctx.user_content.parts:
            user_text = "".join(p.text or "" for p in ctx.user_content.parts)
        turn = int(ctx.session.state.get("turn_count") or 0) + 1
        if self.pause_on_keyword in user_text:
            call_id = "call-review-1"
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                long_running_tool_ids={call_id},
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id=call_id,
                                name="review_research",
                                args={"summary": "Ready for human review"},
                            )
                        )
                    ],
                ),
                actions=EventActions(state_delta={"turn_count": turn}),
            )
            return
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=f"Agent {self.name} turn={turn} sid={ctx.session.id}"
                    )
                ],
            ),
            actions=EventActions(state_delta={"turn_count": turn}),
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
    ],
)
def test_a2a_enabled_env_gating(raw: str | None, expected: bool):
    env = {} if raw is None else {"ENABLE_A2A_SERVER": raw}
    assert a2a_enabled(env) is expected


def test_handoff_token_mint_and_verify():
    secret = "test-handoff-hmac-secret-32-bytes"
    tok = mint_handoff_token(
        "creative_agent", "sid-101", secret=secret, ttl_seconds=600, now=1_000.0
    )
    assert verify_handoff_token(
        tok,
        app_name="creative_agent",
        session_id="sid-101",
        secret=secret,
        now=1_300.0,
    )
    # Expired
    assert not verify_handoff_token(
        tok,
        app_name="creative_agent",
        session_id="sid-101",
        secret=secret,
        now=1_601.0,
    )
    # Wrong session or app or secret
    assert not verify_handoff_token(
        tok,
        app_name="creative_agent",
        session_id="sid-999",
        secret=secret,
        now=1_100.0,
    )
    assert not verify_handoff_token(
        tok,
        app_name="trend_scout",
        session_id="sid-101",
        secret=secret,
        now=1_100.0,
    )
    assert not verify_handoff_token(
        tok,
        app_name="creative_agent",
        session_id="sid-101",
        secret="wrong-secret",
        now=1_100.0,
    )
    # Tampered signature & malformed inputs
    payload_part, sig_part = tok.split(".")
    flipped = ("A" if sig_part[0] != "A" else "B") + sig_part[1:]
    assert not verify_handoff_token(
        f"{payload_part}.{flipped}",
        app_name="creative_agent",
        session_id="sid-101",
        secret=secret,
        now=1_100.0,
    )
    assert not verify_handoff_token(
        "not-a-valid-token",
        app_name="creative_agent",
        session_id="sid-101",
        secret=secret,
        now=1_100.0,
    )
    assert not verify_handoff_token(
        "",
        app_name="creative_agent",
        session_id="sid-101",
        secret=secret,
    )
    # Non-ASCII signature or payload returns False cleanly (never TypeError / 500)
    assert not verify_handoff_token(
        "abc.\u00e9",
        app_name="creative_agent",
        session_id="sid-101",
        secret=secret,
    )
    assert not verify_handoff_token(
        "\u00e9.abc",
        app_name="creative_agent",
        session_id="sid-101",
        secret=secret,
    )


def test_build_studio_url_omits_unclaimable_a2a_service_link_without_secret(caplog):
    # Shared a2a-service session WITHOUT A2A_HANDOFF_SECRET would 404 -> returns None + warning
    assert (
        build_studio_url(
            "https://studio.example.com",
            app_name="creative_agent",
            session_id="sid-1",
            user_id=A2A_SERVICE_USER,
            paused=False,
            handoff_secret="",
        )
        is None
    )
    assert any("A2A_HANDOFF_SECRET is unset" in r.message for r in caplog.records)

    # Directly owned session (valid user_email forwarded) does NOT require A2A_HANDOFF_SECRET
    direct = build_studio_url(
        "https://studio.example.com",
        app_name="interactive_creative",
        session_id="sid-2",
        user_id="alice@example.com",
        paused=True,
        handoff_secret="",
    )
    assert (
        direct
        == "https://studio.example.com/run/sid-2?app=interactive_creative&from=ge"
    )


def test_bounded_in_memory_task_store_evicts_terminal_first_then_oldest():
    store = BoundedInMemoryTaskStore(max_tasks=3)
    ctx = ServerCallContext()

    def _mk_task(tid: str, state: int) -> a2a_pb2.Task:
        t = a2a_pb2.Task(id=tid, context_id=f"c-{tid}")
        t.status.state = state
        return t

    async def _run():
        # Save 1 working, 1 completed, 1 input-required (at capacity = 3)
        await store.save(
            _mk_task("t-working-1", a2a_pb2.TaskState.TASK_STATE_WORKING), ctx
        )
        await store.save(
            _mk_task("t-done-2", a2a_pb2.TaskState.TASK_STATE_COMPLETED), ctx
        )
        await store.save(
            _mk_task("t-paused-3", a2a_pb2.TaskState.TASK_STATE_INPUT_REQUIRED), ctx
        )
        assert len(store) == 3

        # Saving a 4th task evicts oldest TERMINAL task ('t-done-2'), keeping 't-working-1' alive!
        await store.save(
            _mk_task("t-working-4", a2a_pb2.TaskState.TASK_STATE_WORKING), ctx
        )
        assert len(store) == 3
        assert await store.get("t-done-2", ctx) is None
        assert await store.get("t-working-1", ctx) is not None
        assert await store.get("t-paused-3", ctx) is not None
        assert await store.get("t-working-4", ctx) is not None

        # When all 3 in-store tasks are non-terminal, saving a 5th evicts oldest overall ('t-working-1')
        await store.save(
            _mk_task("t-working-5", a2a_pb2.TaskState.TASK_STATE_WORKING), ctx
        )
        assert len(store) == 3
        assert await store.get("t-working-1", ctx) is None
        assert await store.get("t-paused-3", ctx) is not None
        assert await store.get("t-working-5", ctx) is not None

    asyncio.run(_run())


def test_context_session_map_concurrent_resolve_across_multiple_event_loops():
    svc = _VertexStyleSessionService()
    cmap = ContextSessionMap(max_entries=16)

    async def _burst(ctx_id: str) -> list[str]:
        return list(
            await asyncio.gather(
                *[
                    cmap.resolve(
                        svc,
                        app_name="creative_agent",
                        user_id=A2A_SERVICE_USER,
                        context_id=ctx_id,
                        initial_state={"run_origin": "a2a", "ui_app": "creative_agent"},
                    )
                    for _ in range(5)
                ]
            )
        )

    # Loop 1
    sids_loop1 = asyncio.run(_burst("ctx-shared-1"))
    assert len(set(sids_loop1)) == 1
    # Loop 2 (brand new event loop on same ContextSessionMap instance -> no bound-loop RuntimeError)
    sids_loop2 = asyncio.run(_burst("ctx-shared-2"))
    assert len(set(sids_loop2)) == 1
    assert sids_loop1[0] != sids_loop2[0]
    assert len(svc.created_calls) == 2


def test_agent_cards_for_all_three_agents():
    session_service = _VertexStyleSessionService()
    app = FastAPI()

    def _unused_runner_factory(name: str) -> Runner:
        raise AssertionError(f"Card fetch must not instantiate runner ({name})")

    mounted = mount_a2a_apps(
        app,
        runner_factory=_unused_runner_factory,
        session_service=session_service,
        env={"A2A_PUBLIC_BASE_URL": "https://trend-trawler-api.example.com"},
    )
    assert mounted == [
        "/a2a/trend_scout",
        "/a2a/creative_agent",
        "/a2a/interactive_creative",
    ]

    async def _fetch_all():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            for agent_name in DEFAULT_A2A_AGENTS:
                for card_path in (
                    f"/a2a/{agent_name}/.well-known/agent-card.json",
                    f"/a2a/{agent_name}/.well-known/agent.json",
                ):
                    resp = await client.get(card_path)
                    assert resp.status_code == 200, card_path
                    body = resp.json()
                    assert body["name"] == agent_name
                    assert (
                        body["url"]
                        == f"https://trend-trawler-api.example.com/a2a/{agent_name}/"
                    )
                    assert body["capabilities"]["streaming"] is True
                    assert len(body["skills"]) >= 1
                    assert any(
                        iface["url"]
                        == f"https://trend-trawler-api.example.com/a2a/{agent_name}/"
                        for iface in body["supportedInterfaces"]
                    )

    asyncio.run(_fetch_all())
    # Verify build_agent_card + agent_card_dict match offline
    offline_dict = agent_card_dict(
        build_agent_card(
            "creative_agent", base_url="https://trend-trawler-api.example.com"
        )
    )
    assert offline_dict["name"] == "creative_agent"
    assert offline_dict["preferredTransport"] == "JSONRPC"


def _jsonrpc_send_payload(
    text: str,
    *,
    context_id: str,
    message_id: str = "msg-1",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "message": {
            "messageId": message_id,
            "contextId": context_id,
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
        }
    }
    if metadata:
        params["metadata"] = metadata
    return {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "message/send",
        "params": params,
    }


def test_a2a_preserves_app_plugins_resumability_and_reuses_vertex_sessions():
    session_service = _VertexStyleSessionService()
    plugin = _RecordingPlugin()
    built_runners: list[Runner] = []

    def _runner_factory(app_name: str) -> Runner:
        adk_app = App(
            name=app_name,
            root_agent=_StubEchoAgent(name=f"{app_name}_root"),
            plugins=[plugin],
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
        runner = Runner(
            app=adk_app,
            app_name=app_name,
            session_service=session_service,
        )
        built_runners.append(runner)
        return runner

    secret = "handoff-secret-for-a2a-integration"
    env = {
        "A2A_PUBLIC_BASE_URL": "https://trend-trawler-api.example.com",
        "STUDIO_PUBLIC_URL": "https://trend-trawler-studio.example.com",
        "A2A_HANDOFF_SECRET": secret,
    }
    app = FastAPI()
    cmap = ContextSessionMap(max_entries=16)
    mount_a2a_apps(
        app,
        runner_factory=_runner_factory,
        session_service=session_service,
        env=env,
        context_sessions=cmap,
    )

    async def _exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            # Turn 1 (no trailing slash -> direct POST route, no 307)
            r1 = await client.post(
                "/a2a/creative_agent",
                json=_jsonrpc_send_payload(
                    "Draft campaign concepts",
                    context_id="ge-ctx-abc",
                    message_id="m1",
                    metadata={"origin": "gemini_enterprise"},
                ),
            )
            assert r1.status_code == 200
            res1 = r1.json()["result"]
            meta1 = res1["metadata"]
            assert meta1["adk_session_id"] == "vertex-sid-1"
            studio_url = meta1["studio_url"]
            parsed = urlparse(studio_url)
            assert parsed.path == "/results/vertex-sid-1"
            qs = parse_qs(parsed.query)
            assert qs["app"] == ["creative_agent"]
            assert qs["from"] == ["ge"]
            assert verify_handoff_token(
                qs["handoff"][0],
                app_name="creative_agent",
                session_id="vertex-sid-1",
                secret=secret,
            )

            # Turn 2 on SAME context_id reuses vertex-sid-1 without calling create_session again
            r2 = await client.post(
                "/a2a/creative_agent/",
                json=_jsonrpc_send_payload(
                    "Refine headline tone",
                    context_id="ge-ctx-abc",
                    message_id="m2",
                    metadata={"origin": "gemini_enterprise"},
                ),
            )
            assert r2.status_code == 200
            res2 = r2.json()["result"]
            assert res2["metadata"]["adk_session_id"] == "vertex-sid-1"

            # Verify Vertex session creation contract: called once, with session_id=None
            assert len(session_service.created_calls) == 1
            assert session_service.created_calls[0]["session_id"] is None
            assert session_service.created_calls[0]["user_id"] == A2A_SERVICE_USER
            assert session_service.created_calls[0]["state"] == {
                "run_origin": "gemini_enterprise",
                "ui_app": "creative_agent",
            }

            stored = await session_service.get_session(
                app_name="creative_agent",
                user_id=A2A_SERVICE_USER,
                session_id="vertex-sid-1",
            )
            assert stored is not None
            assert stored.state["turn_count"] == 2
            assert stored.state["run_origin"] == "gemini_enterprise"
            assert stored.state["ui_app"] == "creative_agent"

            # Interactive pause -> input-required + /run/<sid> Studio deep-link
            r3 = await client.post(
                "/a2a/interactive_creative/",
                json=_jsonrpc_send_payload(
                    "Start and PAUSE_CHECKPOINT",
                    context_id="ge-ctx-pause",
                    message_id="m3",
                    metadata={
                        "origin": "gemini_enterprise",
                        "user_email": "Alice@Example.com",
                    },
                ),
            )
            assert r3.status_code == 200
            res3 = r3.json()["result"]
            assert res3["status"]["state"] in (
                "input-required",
                "TASK_STATE_INPUT_REQUIRED",
            )
            pause_url = res3["metadata"]["studio_url"]
            pause_parsed = urlparse(pause_url)
            assert pause_parsed.path == "/run/vertex-sid-2"
            pause_qs = parse_qs(pause_parsed.query)
            # Forwarded valid user_email owns the session directly -> no a2a-service handoff token needed
            assert "handoff" not in pause_qs
            owned = await session_service.get_session(
                app_name="interactive_creative",
                user_id="alice@example.com",
                session_id="vertex-sid-2",
            )
            assert owned is not None
            assert owned.state["ui_app"] == "interactive_creative"

    asyncio.run(_exercise())
    assert plugin.run_count == 3
    assert built_runners
    assert all(
        r.resumability_config and r.resumability_config.is_resumable
        for r in built_runners
    )


def test_async_app_wires_conditional_a2a_and_changelog_router():
    import ast
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "deployment" / "async_app.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    guarded_mount = False
    changelog_included = False
    changelog_configured_with_artifacts = False
    middleware_has_a2a = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            cond = ast.unparse(node.test)
            body_src = "\n".join(ast.unparse(stmt) for stmt in node.body)
            if "a2a_enabled()" in cond and "mount_a2a_apps(" in body_src:
                guarded_mount = True
        if isinstance(node, ast.Call):
            call_src = ast.unparse(node)
            if "app.include_router(changelog.router)" in call_src:
                changelog_included = True
            if (
                "changelog.configure(" in call_src
                and "artifact_service=artifact_service" in call_src
            ):
                changelog_configured_with_artifacts = True
            if (
                "app.add_middleware" in call_src
                and "a2a_allowed_sas=" in call_src
                and "a2a_public_agent_card=" in call_src
            ):
                middleware_has_a2a = True
    assert guarded_mount
    assert changelog_included
    assert changelog_configured_with_artifacts
    assert middleware_has_a2a
