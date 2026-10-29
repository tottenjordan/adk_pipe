"""Shared-session, ``App``-preserving multi-agent A2A server mounts.

Exposes ``trend_scout``, ``creative_agent``, and ``interactive_creative`` under
``/a2a/<agent_name>`` on the existing FastAPI ``async_app`` when
``ENABLE_A2A_SERVER`` is truthy. Unlike bare ``to_a2a(agent)`` — which creates an
isolated ``InMemorySessionService``, drops ``App``-level ``ResumabilityConfig``
and ``ScopedModelArmorPlugin``, and mounts only at ``/`` — this module builds each
``A2aAgentExecutor`` around ``async_app``'s shared ``runner_factory`` and
``session_service``:

- ``App`` plugins and ``ResumabilityConfig(is_resumable=True)`` stay active, and
  ``force_new_version=True`` maps ``LongRunningFunctionTool`` pauses to
  ``TaskState.TASK_STATE_INPUT_REQUIRED``.
- New A2A conversations create ADK sessions with ``session_id=None`` so
  ``VertexAiSessionService`` mints valid session IDs instead of rejecting
  client-supplied ``context_id`` values with ``400 INVALID_ARGUMENT``. A bounded
  LRU maps ``(app_name, user_id, context_id) -> adk_session_id`` for follow-up
  turns in the same A2A ``context_id``.
- Sessions created via A2A record ``run_origin`` (``"gemini_enterprise"`` or
  ``"a2a"``) and emit a terminal ``__runserver__`` status marker plus an optional
  Studio deep-link (with a signed HMAC handoff token when running under the
  shared ``a2a-service`` principal).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.parse
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import OwnerResolver, resolve_user_scope
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskStatusUpdateEvent,
    a2a_pb2,
)
from fastapi import FastAPI
from google.adk.a2a import _compat
from google.adk.a2a.converters.part_converter import (
    A2APartToGenAIPartConverter,
    convert_a2a_part_to_genai_part,
)
from google.adk.a2a.converters.request_converter import (
    A2A_METADATA_KEY,
    AgentRunRequest,
)
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.executor.config import A2aAgentExecutorConfig, ExecuteInterceptor
from google.adk.a2a.executor.executor_context import ExecutorContext
from google.adk.agents.run_config import RunConfig
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.runners import Runner
from google.genai import types as genai_types
from starlette.applications import Starlette
from starlette.routing import Route

from runserver.async_runs import build_terminal_event
from runserver.authz import normalize_user_id

log = logging.getLogger(__name__)

A2A_SERVICE_USER = "a2a-service"
DEFAULT_A2A_AGENTS: tuple[str, ...] = (
    "trend_scout",
    "creative_agent",
    "interactive_creative",
)
DEFAULT_HANDOFF_TTL_SECONDS = 86400
_TRUTHY = frozenset({"1", "true", "yes"})
_STATE_USER_KEY = "tt_a2a_user_id"
_STATE_SESSION_KEY = "tt_a2a_session_id"
_STATE_ORIGIN_KEY = "tt_a2a_run_origin"

_USER_META_KEYS = ("user_email", "userEmail", "email", "user_id", "userId")
_USER_HEADER_KEYS = ("x-goog-authenticated-user-email", "x-forwarded-email")
_ORIGIN_META_KEYS = ("origin", "run_origin", "runOrigin", "source", "client", "caller")
_GE_TOKENS = (
    "gemini_enterprise",
    "gemini-enterprise",
    "geminienterprise",
    "discoveryengine",
    "discovery_engine",
    "discovery-engine",
    "agentspace",
    "ge",
)

_AGENT_METADATA: dict[str, dict[str, Any]] = {
    "trend_scout": {
        "description": (
            "Discovers daily Google Search trends from BigQuery, researches cultural "
            "context on the web, and selects the top campaign-relevant trends."
        ),
        "skills": [
            {
                "id": "discover_campaign_trends",
                "name": "Discover Campaign Search Trends",
                "description": (
                    "Fetches the top 25 daily Google Search trends, investigates why "
                    "each trend is surging, and filters to the 3 strongest cultural "
                    "matches for a brand, product, and target audience."
                ),
                "tags": ["trends", "google-trends", "marketing", "research"],
                "examples": [
                    "Find 3 culturally relevant Google Search trends for our electric guitar campaign.",
                    "Scout today's rising search trends for a sustainable running shoe launch.",
                ],
            }
        ],
    },
    "creative_agent": {
        "description": (
            "Runs parallel brand and trend research, synthesizes a cited strategic "
            "brief, drafts and gates ad copy and visual concepts, renders ad images, "
            "and evaluates every creative with a 12-dimension LLM judge."
        ),
        "skills": [
            {
                "id": "generate_evaluated_ad_creatives",
                "name": "Generate & Evaluate Trend-Driven Ad Creatives",
                "description": (
                    "Produces a cited research report, structured CreativeBrief, 4 "
                    "gated ad copies, 4 rendered ad visuals, and a 12-dimension "
                    "CreativeEvaluationReport for a <trend, campaign> pair."
                ),
                "tags": ["advertising", "creative-generation", "evaluation", "brief"],
                "examples": [
                    "Create and evaluate ad copy and visuals connecting our guitar campaign to 'acoustic session'.",
                ],
            }
        ],
    },
    "interactive_creative": {
        "description": (
            "Human-in-the-loop creative pipeline that pauses at three review "
            "checkpoints (creative brief, ad copy, and visual concepts) before "
            "rendering images and running the 12-dimension creative judge."
        ),
        "skills": [
            {
                "id": "interactive_creative_production",
                "name": "Interactive Trend-to-Creative Studio Workflow",
                "description": (
                    "Synthesizes research and a structured creative brief, pauses for "
                    "human approval or edits at brief, copy, and visual concept "
                    "checkpoints, then renders and scores the final portfolio."
                ),
                "tags": [
                    "advertising",
                    "human-in-the-loop",
                    "creative-brief",
                    "interactive",
                ],
                "examples": [
                    "Start an interactive creative run with human review checkpoints for our campaign.",
                ],
            }
        ],
    },
}


def a2a_enabled(env: Mapping[str, str] = os.environ) -> bool:
    """Return True when ``ENABLE_A2A_SERVER`` is set to ``1``, ``true``, or ``yes``."""
    return env.get("ENABLE_A2A_SERVER", "").strip().lower() in _TRUTHY


def resolve_public_base_url(env: Mapping[str, str] = os.environ) -> str:
    """Public base URL advertised in A2A Agent Cards (no trailing slash)."""
    raw = (
        env.get("A2A_PUBLIC_BASE_URL")
        or env.get("BACKEND_PUBLIC_URL")
        or "http://localhost:8000"
    )
    return raw.strip().rstrip("/") or "http://localhost:8000"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def mint_handoff_token(
    app_name: str,
    session_id: str,
    *,
    secret: str,
    ttl_seconds: int = DEFAULT_HANDOFF_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Mint a URL-safe HMAC-SHA256 handoff token binding ``(app_name, session_id, exp)``."""
    if not secret or not secret.strip():
        raise ValueError("secret must be non-empty")
    if not app_name or not session_id:
        raise ValueError("app_name and session_id must be non-empty")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    ts = time.time() if now is None else float(now)
    payload = json.dumps(
        {"app": app_name, "sid": session_id, "exp": int(ts + ttl_seconds)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload_b64 = _b64url_encode(payload)
    sig = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify_handoff_token(
    token: str,
    *,
    app_name: str,
    session_id: str,
    secret: str,
    now: float | None = None,
) -> bool:
    """Verify an HMAC-SHA256 handoff token against ``app_name``, ``session_id``, and expiry."""
    if not token or not secret or not secret.strip():
        return False
    parts = token.strip().split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    payload_b64, sig_b64 = parts
    try:
        payload_b64_bytes = payload_b64.encode("ascii")
        sig_b64_bytes = sig_b64.encode("ascii")
    except UnicodeEncodeError:
        return False
    expected_sig_bytes = base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), payload_b64_bytes, hashlib.sha256).digest()
    ).rstrip(b"=")
    if not hmac.compare_digest(expected_sig_bytes, sig_b64_bytes):
        return False
    try:
        data = json.loads(_b64url_decode(payload_b64))
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(data, Mapping):
        return False
    exp = data.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return False
    ts = time.time() if now is None else float(now)
    if ts > float(exp):
        return False
    return data.get("app") == app_name and data.get("sid") == session_id


def build_studio_url(
    studio_public_url: str | None,
    *,
    app_name: str,
    session_id: str,
    user_id: str,
    paused: bool,
    handoff_secret: str | None = None,
    handoff_ttl_seconds: int = DEFAULT_HANDOFF_TTL_SECONDS,
) -> str | None:
    """Build a Studio deep-link to ``/results/<sid>`` (or ``/run/<sid>`` when paused)."""
    if not studio_public_url or not studio_public_url.strip():
        return None
    base = studio_public_url.strip().rstrip("/")
    page = "run" if paused else "results"
    sid_enc = urllib.parse.quote(session_id, safe="")
    params: list[tuple[str, str]] = [("app", app_name), ("from", "ge")]
    secret = (handoff_secret or "").strip()
    if user_id == A2A_SERVICE_USER:
        if not secret:
            log.warning(
                "a2a: omitting studio_url for %s/%s under %s because A2A_HANDOFF_SECRET is unset",
                app_name,
                session_id,
                A2A_SERVICE_USER,
            )
            return None
        token = mint_handoff_token(
            app_name,
            session_id,
            secret=secret,
            ttl_seconds=handoff_ttl_seconds,
        )
        params.append(("handoff", token))
    query = urllib.parse.urlencode(params)
    return f"{base}/{page}/{sid_enc}?{query}"


def build_agent_card(
    agent_name: str,
    *,
    base_url: str = "http://localhost:8000",
    version: str = "0.1.0",
) -> AgentCard:
    """Build a deterministic A2A ``AgentCard`` (dual v1.0 + v0.3 compatible)."""
    spec = _AGENT_METADATA.get(
        agent_name,
        {
            "description": f"Trend Trawler ADK agent '{agent_name}'.",
            "skills": [
                {
                    "id": agent_name,
                    "name": agent_name.replace("_", " ").title(),
                    "description": f"Execute the {agent_name} workflow.",
                    "tags": ["trend-trawler", agent_name],
                    "examples": [f"Run {agent_name}."],
                }
            ],
        },
    )
    rpc_url = f"{base_url.rstrip('/')}/a2a/{agent_name}/"
    skills = [
        AgentSkill(
            id=s["id"],
            name=s["name"],
            description=s["description"],
            tags=list(s["tags"]),
            examples=list(s["examples"]),
        )
        for s in spec["skills"]
    ]
    return AgentCard(
        name=agent_name,
        description=spec["description"],
        version=version,
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                url=rpc_url, protocol_binding="JSONRPC", protocol_version="1.0"
            ),
            AgentInterface(
                url=rpc_url, protocol_binding="JSONRPC", protocol_version="0.3.0"
            ),
        ],
        skills=skills,
    )


def agent_card_dict(card: AgentCard) -> dict[str, Any]:
    """Serialize ``card`` to the exact JSON dict served at ``/.well-known/agent-card.json``."""
    return agent_card_to_dict(card)


def _request_meta_dicts(context: RequestContext) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    req_meta = _compat.meta_to_dict(getattr(context, "metadata", None))
    if req_meta:
        out.append(req_meta)
    msg = getattr(context, "message", None)
    if msg is not None:
        msg_meta = _compat.meta_to_dict(getattr(msg, "metadata", None))
        if msg_meta:
            out.append(msg_meta)
    return out


def _context_headers(context: RequestContext) -> Mapping[str, str]:
    call_ctx = getattr(context, "call_context", None)
    state = getattr(call_ctx, "state", None)
    headers = state.get("headers") if isinstance(state, Mapping) else None
    if not isinstance(headers, Mapping):
        return {}
    return {
        str(k).lower(): str(v)
        for k, v in headers.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def extract_a2a_user_id(context: RequestContext) -> str:
    """Extract normalized end-user email from A2A metadata/context, or ``a2a-service``."""
    candidates: list[str] = []
    for meta in _request_meta_dicts(context):
        for key in _USER_META_KEYS:
            val = meta.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val)
    call_ctx = getattr(context, "call_context", None)
    user = getattr(call_ctx, "user", None)
    if (
        user is not None
        and getattr(user, "is_authenticated", False)
        and isinstance(getattr(user, "user_name", None), str)
    ):
        candidates.append(user.user_name)
    headers = _context_headers(context)
    for key in _USER_HEADER_KEYS:
        if val := headers.get(key):
            candidates.append(val)
    for raw in candidates:
        try:
            return normalize_user_id(raw)
        except ValueError:
            continue
    return A2A_SERVICE_USER


def _matches_ge_marker(raw: str) -> bool:
    norm = raw.strip().lower()
    if norm == "ge":
        return True
    return any(tok != "ge" and tok in norm for tok in _GE_TOKENS)


def extract_run_origin(context: RequestContext) -> str:
    """Return ``'gemini_enterprise'`` when metadata/headers mark GE, else ``'a2a'``."""
    for meta in _request_meta_dicts(context):
        for key in _ORIGIN_META_KEYS:
            val = meta.get(key)
            if isinstance(val, str) and _matches_ge_marker(val):
                return "gemini_enterprise"
        if (
            meta.get("gemini_enterprise") is True
            or meta.get("geminiEnterprise") is True
        ):
            return "gemini_enterprise"
    headers = _context_headers(context)
    if headers.get("x-gemini-enterprise") or headers.get("x-goog-discoveryengine-app"):
        return "gemini_enterprise"
    ua = headers.get("user-agent", "")
    if ua and _matches_ge_marker(ua):
        return "gemini_enterprise"
    return "a2a"


async def _get_session_safe(
    session_service: Any, app_name: str, user_id: str, session_id: str
) -> Any | None:
    try:
        return await session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
    except (SessionNotFoundError, ValueError):
        return None


_TERMINAL_TASK_STATES = frozenset(
    s
    for s in (
        _compat.TS_COMPLETED,
        _compat.TS_FAILED,
        _compat.TS_CANCELED,
        getattr(a2a_pb2.TaskState, "TASK_STATE_REJECTED", None),
        "completed",
        "failed",
        "canceled",
        "rejected",
    )
    if s is not None
)


class BoundedInMemoryTaskStore(TaskStore):
    """Bounded ``TaskStore`` wrapper around ``InMemoryTaskStore`` with terminal-first LRU eviction."""

    def __init__(
        self,
        max_tasks: int = 1024,
        *,
        owner_resolver: OwnerResolver = resolve_user_scope,
    ) -> None:
        self._max_tasks = max(1, max_tasks)
        self._owner_resolver = owner_resolver
        self._inner = InMemoryTaskStore(owner_resolver=owner_resolver)
        self._order: OrderedDict[tuple[str, str], tuple[bool, ServerCallContext]] = (
            OrderedDict()
        )
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._order)

    async def save(self, task: a2a_pb2.Task, context: ServerCallContext) -> None:
        await self._inner.save(task, context)
        owner = self._owner_resolver(context)
        state_val = getattr(getattr(task, "status", None), "state", None)
        terminal = state_val in _TERMINAL_TASK_STATES
        key = (owner, task.id)
        to_evict: list[tuple[str, ServerCallContext]] = []
        with self._lock:
            self._order[key] = (terminal, context)
            self._order.move_to_end(key)
            while len(self._order) > self._max_tasks:
                evict_key = next(
                    (
                        k
                        for k, (is_term, _ctx) in self._order.items()
                        if is_term and k != key
                    ),
                    None,
                )
                if evict_key is None:
                    evict_key = next(iter(self._order))
                _is_term, evict_ctx = self._order.pop(evict_key)
                to_evict.append((evict_key[1], evict_ctx))
        for evict_task_id, evict_ctx in to_evict:
            await self._inner.delete(evict_task_id, evict_ctx)

    async def get(
        self, task_id: str, context: ServerCallContext
    ) -> a2a_pb2.Task | None:
        return await self._inner.get(task_id, context)

    async def list(
        self,
        params: a2a_pb2.ListTasksRequest,
        context: ServerCallContext,
    ) -> a2a_pb2.ListTasksResponse:
        return await self._inner.list(params, context)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        owner = self._owner_resolver(context)
        with self._lock:
            self._order.pop((owner, task_id), None)
        await self._inner.delete(task_id, context)


class ContextSessionMap:
    """Bounded thread/async-safe LRU mapping ``(app_name, user_id, context_id) -> adk_session_id``."""

    def __init__(self, max_entries: int = 2048) -> None:
        self._max_entries = max(1, max_entries)
        self._entries: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._lock = threading.RLock()
        self._resolve_locks: weakref.WeakValueDictionary[
            tuple[int, str, str, str], asyncio.Lock
        ] = weakref.WeakValueDictionary()

    def _get_resolve_lock(
        self, app_name: str, user_id: str, context_id: str
    ) -> asyncio.Lock:
        loop_id = id(asyncio.get_running_loop())
        key = (loop_id, app_name, user_id, context_id)
        with self._lock:
            lock = self._resolve_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._resolve_locks[key] = lock
            return lock

    def get(self, app_name: str, user_id: str, context_id: str) -> str | None:
        key = (app_name, user_id, context_id)
        with self._lock:
            sid = self._entries.get(key)
            if sid is not None:
                self._entries.move_to_end(key)
            return sid

    def put(
        self, app_name: str, user_id: str, context_id: str, session_id: str
    ) -> None:
        key = (app_name, user_id, context_id)
        with self._lock:
            self._entries[key] = session_id
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    async def resolve(
        self,
        session_service: Any,
        *,
        app_name: str,
        user_id: str,
        context_id: str | None,
        initial_state: Mapping[str, Any],
    ) -> str:
        """Return an existing ADK ``session.id`` for ``context_id`` or create one with ``session_id=None``."""
        if not context_id:
            created = await session_service.create_session(
                app_name=app_name,
                user_id=user_id,
                state=dict(initial_state),
                session_id=None,
            )
            return str(created.id)

        resolve_lock = self._get_resolve_lock(app_name, user_id, context_id)
        async with resolve_lock:
            cached = self.get(app_name, user_id, context_id)
            if cached is not None:
                existing = await _get_session_safe(
                    session_service, app_name, user_id, cached
                )
                if existing is not None:
                    return str(existing.id)
            direct = await _get_session_safe(
                session_service, app_name, user_id, context_id
            )
            if direct is not None:
                sid = str(direct.id)
                self.put(app_name, user_id, context_id, sid)
                return sid
            created = await session_service.create_session(
                app_name=app_name,
                user_id=user_id,
                state=dict(initial_state),
                session_id=None,
            )
            sid = str(created.id)
            self.put(app_name, user_id, context_id, sid)
            return sid


def _append_studio_link_to_status(
    final_event: TaskStatusUpdateEvent, studio_url: str, *, paused: bool
) -> None:
    label = (
        f"Review checkpoint ready in Trend Trawler Studio: {studio_url}"
        if paused
        else f"View full results in Trend Trawler Studio: {studio_url}"
    )
    link_part = _compat.make_text_part(label)
    status = final_event.status
    if _compat.IS_A2A_V1:
        if status.HasField("message"):
            status.message.parts.append(link_part)
        else:
            msg = _compat.make_message(
                message_id=str(uuid.uuid4()),
                role="agent",
                parts=[link_part],
            )
            status.message.CopyFrom(msg)
    else:
        existing_msg = getattr(status, "message", None)
        if (
            existing_msg is not None
            and getattr(existing_msg, "parts", None) is not None
        ):
            existing_msg.parts.append(link_part)
        else:
            status.message = _compat.make_message(
                message_id=str(uuid.uuid4()),
                role="agent",
                parts=[link_part],
            )


def build_a2a_executor_config(
    *,
    agent_name: str,
    session_service: Any,
    context_sessions: ContextSessionMap | None = None,
    env: Mapping[str, str] = os.environ,
) -> A2aAgentExecutorConfig:
    """Build ``A2aAgentExecutorConfig`` with Vertex-safe session resolution and Studio handoff links."""
    cmap = context_sessions if context_sessions is not None else ContextSessionMap()

    async def _before_agent(context: RequestContext) -> RequestContext:
        user_id = extract_a2a_user_id(context)
        run_origin = extract_run_origin(context)
        adk_session_id = await cmap.resolve(
            session_service,
            app_name=agent_name,
            user_id=user_id,
            context_id=context.context_id,
            initial_state={"run_origin": run_origin, "ui_app": agent_name},
        )
        if context.call_context is not None:
            context.call_context.state[_STATE_USER_KEY] = user_id
            context.call_context.state[_STATE_SESSION_KEY] = adk_session_id
            context.call_context.state[_STATE_ORIGIN_KEY] = run_origin
        return context

    def _request_converter(
        request: RequestContext,
        part_converter: A2APartToGenAIPartConverter = convert_a2a_part_to_genai_part,
    ) -> AgentRunRequest:
        if not request.message:
            raise ValueError("Request message cannot be None")
        state = (
            request.call_context.state
            if request.call_context is not None
            and isinstance(request.call_context.state, Mapping)
            else {}
        )
        user_id = state.get(_STATE_USER_KEY) or extract_a2a_user_id(request)
        run_origin = state.get(_STATE_ORIGIN_KEY) or extract_run_origin(request)
        session_id = state.get(_STATE_SESSION_KEY)
        if not session_id and request.context_id:
            session_id = cmap.get(agent_name, user_id, request.context_id)
        state_delta = (
            {"run_origin": run_origin, "ui_app": agent_name} if not session_id else None
        )

        custom_metadata: dict[str, Any] = {}
        request_metadata = _compat.meta_to_dict(request.metadata)
        if request_metadata:
            custom_metadata[A2A_METADATA_KEY] = request_metadata

        output_parts: list[genai_types.Part] = []
        for a2a_part in request.message.parts:
            genai_parts = part_converter(a2a_part)
            if not isinstance(genai_parts, list):
                genai_parts = [genai_parts] if genai_parts else []
            output_parts.extend(genai_parts)

        return AgentRunRequest(
            user_id=user_id,
            session_id=session_id,
            new_message=genai_types.Content(role="user", parts=output_parts),
            state_delta=state_delta,
            run_config=RunConfig(custom_metadata=custom_metadata),
        )

    async def _after_agent(
        executor_context: ExecutorContext,
        final_event: TaskStatusUpdateEvent,
    ) -> TaskStatusUpdateEvent:
        sid = executor_context.session_id
        uid = executor_context.user_id
        state_val = final_event.status.state
        paused = state_val == _compat.TS_INPUT_REQUIRED
        failed = state_val == _compat.TS_FAILED

        session = await _get_session_safe(session_service, agent_name, uid, sid)
        if session is not None:
            marker = build_terminal_event("error" if failed else "done")
            try:
                await session_service.append_event(session, marker)
            except Exception:
                log.warning(
                    "a2a: failed to append terminal status marker for %s/%s",
                    agent_name,
                    sid,
                    exc_info=True,
                )

        studio_url = build_studio_url(
            env.get("STUDIO_PUBLIC_URL"),
            app_name=agent_name,
            session_id=sid,
            user_id=uid,
            paused=paused,
            handoff_secret=env.get("A2A_HANDOFF_SECRET"),
        )
        merged_meta = dict(_compat.meta_to_dict(final_event.metadata))
        merged_meta["adk_session_id"] = sid
        if studio_url:
            merged_meta["studio_url"] = studio_url
            _append_studio_link_to_status(final_event, studio_url, paused=paused)
        _compat.set_event_metadata(final_event, merged_meta)
        return final_event

    return A2aAgentExecutorConfig(
        request_converter=_request_converter,
        execute_interceptors=[
            ExecuteInterceptor(
                before_agent=_before_agent,
                after_agent=_after_agent,
            )
        ],
    )


def build_a2a_subapp(
    *,
    agent_name: str,
    runner_factory: Callable[[str], Runner],
    session_service: Any,
    base_url: str,
    context_sessions: ContextSessionMap | None = None,
    env: Mapping[str, str] = os.environ,
) -> tuple[Starlette, list[Route], AgentCard]:
    """Build the Starlette A2A sub-app, its JSON-RPC routes, and ``AgentCard`` for one agent."""
    card = build_agent_card(agent_name, base_url=base_url)
    config = build_a2a_executor_config(
        agent_name=agent_name,
        session_service=session_service,
        context_sessions=context_sessions,
        env=env,
    )
    executor = A2aAgentExecutor(
        runner=lambda name=agent_name: runner_factory(name),
        config=config,
        force_new_version=True,
    )
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=BoundedInMemoryTaskStore(),
        agent_card=card,
    )
    rpc_routes = create_jsonrpc_routes(
        handler,
        rpc_url="/",
        enable_v0_3_compat=True,
    )
    routes: list[Route] = [
        *create_agent_card_routes(card, card_url="/.well-known/agent-card.json"),
        *create_agent_card_routes(card, card_url="/.well-known/agent.json"),
        *rpc_routes,
    ]
    return Starlette(routes=routes), rpc_routes, card


def mount_a2a_apps(
    app: FastAPI,
    *,
    runner_factory: Callable[[str], Runner],
    session_service: Any,
    agent_names: Sequence[str] = DEFAULT_A2A_AGENTS,
    env: Mapping[str, str] = os.environ,
    context_sessions: ContextSessionMap | None = None,
) -> list[str]:
    """Mount A2A sub-apps at ``/a2a/<agent_name>`` for each agent in ``agent_names``."""
    base_url = resolve_public_base_url(env)
    cmap = context_sessions if context_sessions is not None else ContextSessionMap()
    mounted: list[str] = []
    for agent_name in agent_names:
        prefix = f"/a2a/{agent_name}"
        sub_app, rpc_routes, _card = build_a2a_subapp(
            agent_name=agent_name,
            runner_factory=runner_factory,
            session_service=session_service,
            base_url=base_url,
            context_sessions=cmap,
            env=env,
        )
        # Accept POST /a2a/<agent_name> without a trailing slash directly (no 307)
        # in addition to the mounted Starlette sub-app at /a2a/<agent_name>/.
        for route in rpc_routes:
            if route.methods and "POST" in route.methods:
                app.add_route(
                    prefix,
                    route.endpoint,
                    methods=["POST"],
                    include_in_schema=False,
                )
        app.mount(prefix, sub_app, name=f"a2a_{agent_name}")
        mounted.append(prefix)
    log.info("mounted A2A endpoints (base_url=%s): %s", base_url, ", ".join(mounted))
    return mounted
