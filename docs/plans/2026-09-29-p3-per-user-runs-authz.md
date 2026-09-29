# P3: Per-User Authorization for /runs + Session Routes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) to implement this plan task-by-task.

**Status:** complete 2026-09-29 (PR #184; live: api enforce, web proxy scoping; cross-user checks verified) — executed via 2026-09-29-p3-execution.md
**Goal:** Each IAP user can only create, read, poll, or resume their own sessions. Today any IAP-authenticated user who knows (or guesses) a `userId` + `sessionId` pair can read, poll, or resume that session.
**Architecture:** The Next.js `/api/adk` proxy is the single authoritative place that turns the browser's IAP identity into a user id. It verifies `X-Goog-IAP-JWT-Assertion` (ES256, issuer, Cloud Run audience), rewrites every `userId` (URL path and `POST /runs` body) to the normalized email, only forwards an allowlist of routes, and sends the id as `X-TT-User`. The backend trusts `X-TT-User` only when the request's Cloud Run-verified `Authorization` ID token belongs to `tt-web-sa`. It returns 403 when the path or body `userId` doesn't match, and it blocks the canned routes the frontend never uses.
**Tech Stack:** Python 3.13, FastAPI/Starlette ASGI middleware, `google-auth` (`google.auth.jwt`), `google-adk` 2.10 `VertexAiSessionService`, Next.js 16 route handlers, `jose` (JWKS/ES256), pytest + httpx `ASGITransport`, Vitest.

**Conventions:** Test snippets below say "append"; hoist their imports to the top of the test module (ruff `E402`/`I`), then run `uv run ruff format`. Follow `CODE_STANDARDS.md` (`uv run …`, `ruff`, `ty`, pytest; `npm test`, `npm run lint`, `npx tsc --noEmit` in `frontend/`). Branch off `main` (e.g. `feat/p3-per-user-authz`), one commit per task. **Commit messages must NEVER include `Co-Authored-By` trailers or any AI/"Generated with" attribution. This overrides any tool default.** Python tests need `GOOGLE_CLOUD_PROJECT=test-project`, which the repo `.env` normally provides.

---

## Current state / threat model

Every user-scoped key is `(app, userId, sessionId)`. The frontend mints `userId = user_${Date.now()}` for each campaign (`frontend/src/app/page.tsx:85`) and carries it in the page URL (`?userId=…`, defaulting to `default_user` in `run/` and `results/`). The proxy (`frontend/src/app/api/adk/[...path]/route.ts`) forwards the path and body verbatim, after stripping IAP headers and attaching a `tt-web-sa` ID token. `VertexAiSessionService.get_session` checks `session.user_id == user_id` (`vertex_ai_session_service.py:294`), but the caller chooses `user_id`, so it works like a guessable bearer secret: millisecond timestamps are easy to guess, and anyone who has the URL has it. An ownership mismatch raises a bare `ValueError`, which the canned app returns as a **500**.

| Endpoint (backend path) | Used by frontend | `userId` source today | Risk |
|---|---|---|---|
| `POST /apps/{app}/users/{user}/sessions` | `createSession` | path, client-minted | Create sessions under any user (low) |
| `GET /apps/{app}/users/{user}/sessions` | `listSessions` | path | **Enumerate** every session of any user id (medium) |
| `GET /apps/{app}/users/{user}/sessions/{sid}` | `getSession` | path (from URL `?userId=`) | Read full state/events of another user's run (high) |
| `GET …/sessions/{sid}/artifacts[/{name}]` | `listArtifacts`, `getArtifact` | path | Read another user's artifacts (high) |
| `POST /runs/{app}` | `startRun` | **body** `userId` | Start or inject a run in someone else's session (high) |
| `GET /runs/{app}/{user}/{sid}?since=N` | `getRunStatus`, `pollRun` | path | Read another user's live event stream and state (high) |
| `POST /runs/{app}/{user}/{sid}/resume` | `resumeRun` | path | **Approve or steer** another user's interactive checkpoint (high) |
| `POST/PATCH/DELETE …/sessions/{sid}`, `POST/DELETE …/artifacts…`, `…/artifacts/{name}/versions…` | no | path | Modify or delete other users' data (high) |
| `POST /run`, `POST /run_sse`, WS `/run_live` | no | body/query `user_id` | Bypasses `/runs` entirely (high) |
| `PATCH /apps/{app}/users/{user}/memory`, `POST /agent-identity/finalize` | no | path / n/a | Unused surface (medium) |
| `GET /list-apps`, `/health`, `/version`, `/apps/{app}/app-info` | `listApps` | none | None |

This is the complete route list from `get_fast_api_app(agents_dir="agents", web=False)` on ADK 2.10 plus `runserver/async_runs.py:694-749`. Out of scope, recorded as follow-up P3b: `/api/gcs` (`frontend/src/app/api/gcs/route.ts`) proxies any `bucket`+`path` that `tt-web-sa` can read. It isn't user-scoped.

## IAP identity facts (verified, with sources)

1. **Headers:** IAP adds `x-goog-iap-jwt-assertion` (signed JWT) and the unsigned `x-goog-authenticated-user-email` / `x-goog-authenticated-user-id` headers, with values like `accounts.google.com:user@example.com`. Google warns: *"If an attacker bypasses IAP, the attacker can forge the IAP unsigned identity headers"*. Use the JWT. Source: https://docs.cloud.google.com/iap/docs/signed-headers-howto
2. **JWT claims:** header `alg` = `ES256`, plus a `kid`. `iss` must be `https://cloud.google.com/iap`. `exp` must be in the future and `iat` in the past (30 s skew allowed). The payload includes `email` and `sub`. Keys: `https://www.gstatic.com/iap/verify/public_key-jwk` (JWK) or `…/public_key` (PEM). Source: same page.
3. **Cloud Run audience (direct IAP, no load balancer):** `/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME`. For us that is `/projects/PROJECT_NUMBER/locations/us-central1/services/trend-trawler-web`. The load-balancer form (`/projects/N/global/backendServices/ID`) does **not** apply. Sources: https://docs.cloud.google.com/iap/docs/signed-headers-howto; cross-checked in https://zenn.dev/msd05keisuke/articles/4a0212035d284d and https://oneuptime.com/blog/post/2026-02-17-how-to-configure-spring-security-with-identity-aware-proxy-for-a-spring-boot-app-on-cloud-run/view (both third-party). **Task 8 Phase B checks this against a real token.**
4. **IAP runs before IAM on Cloud Run.** Its service agent needs `roles/run.invoker`. Pub/Sub-style callers that bring their own auth may fail. Source: https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run. Live (read-only `get-iam-policy`): web invoker = `service-PROJECT_NUMBER@gcp-sa-iap` + `user:admin@example.com`. Api invoker = **only** `tt-web-sa`, and api ingress is `all`, gated by IAM.
5. **Service-to-service tokens:** Cloud Run checks `X-Serverless-Authorization` in preference to `Authorization`, and *"removes the signature before passing the token to the user container"* for that header. Receivers can *"decode the token to get its information"*, e.g. the caller's `email`. Source: https://docs.cloud.google.com/run/docs/authenticating/service-to-service. Our proxy sends `Authorization: Bearer <tt-web-sa ID token, aud = api origin>` and strips `X-Serverless-Authorization`, so the api container receives a full, verifiable Google ID token.
6. The Google Developer Knowledge MCP was unavailable during research (`mcp-remote: Incompatible auth server`). The facts above come from WebFetch of the official pages.

## Design decision

**Choose (a): the proxy is authoritative, and the backend trusts `X-TT-User` only from `tt-web-sa`.**

- The IAP JWT reaches the **web** container, and its audience names the **web** service. That is the natural place to verify it. With option (b), the backend would have to verify a token issued to a different service. Then any `run.invoker` holder who replays a captured assertion (valid about 10 min) could act as that user, and web-specific configuration would leak into the api.
- The backend check stays cheap and works offline. It verifies the forwarded Google ID token (Google OAuth certs, cached for 1 h) and requires `aud ∈ TRUSTED_PROXY_AUDIENCES` and `email == TRUSTED_PROXY_SA`. Cloud Run IAM already enforces this at the edge. Re-verifying in the app is defense in depth in case the api is ever deployed `--allow-unauthenticated` by mistake, or a project Owner or Editor calls it directly.
- **Reject rather than rewrite on the backend.** Only the proxy rewrites. The backend returns 403 when the path or body `userId` differs from `X-TT-User`, 401 when `X-TT-User` is missing or untrusted on a user-scoped route, and 404 for blocked canned routes. An ownership `ValueError` from `VertexAiSessionService` becomes a 404 instead of a 500.
- **Normalization (both sides, same rules):** strip, lowercase, drop the `accounts.google.com:` prefix, and require an email shape. The id is the **email** because it is readable in the Agent Engine console, and `list_sessions` filters on `user_id`. `sub` would be more stable if an email is renamed or reused. That is an open question.
- **Modes (backend):** `TRUST_CLIENT_USER_ID=1` gives today's behavior for local dev only. The app refuses to boot with it when `K_SERVICE` is set, i.e. on Cloud Run. `USER_AUTHZ_MODE=observe` logs mismatches without enforcing, for rollout. The default is `enforce`. **Proxy:** when `K_SERVICE` is set, a missing or invalid IAP JWT gets a 401. Locally, with no JWT, requests pass through unchanged, so no web env vars are needed. The audience comes from the metadata server plus `K_SERVICE`, and `IAP_AUDIENCE` can override it.
- **Frontend clients** send the placeholder `userId = "me"` (`SELF_USER_ID`). The proxy replaces it. The `?userId=` URL param becomes cosmetic.
- **Back-compat / migration:** Agent Engine `user_id` is immutable, so sessions can't be re-owned. Legacy `user_<ts>` sessions become unreachable from the UI after the proxy ships. Their outputs remain in GCS/BQ (`trend_creatives`, `creative_evals`, gallery/PDF). There are no live users yet, so we accept this and run Phase B when no run is in flight. An admin can still read legacy sessions with the SDK (snippet in Task 8).
- **CRF batch worker: not affected.** `cloud_functions/creative_fanout/main.py:303,353` calls the *deployed creative_agent Agent Engine* (`agent_engines.get(...)`, its own session store) under `crf_worker_<index>`. It never touches `trend-trawler-api` or the `trend-trawler-sessions` engine. The same goes for `deployment/integration_test.py` and `test_deployment.py`. No change is needed.

---

## Task 0: Pre-flight (no code)

1. Record rollback targets:
   `gcloud run services describe trend-trawler-api --region us-central1 --format='value(status.traffic)'` (currently `trend-trawler-api-00050-hlh`, tag `main-clean`).
   Run the same for `trend-trawler-web` (currently `00020-2cv`, auto-routed `latestRevision:true`, tag `main-current` on `00017-sqg`).
2. Confirm Agent Engine accepts an email `user_id`. Create and delete a probe session:
   ```bash
   GOOGLE_CLOUD_PROJECT=PROJECT_ID uv run python - <<'EOF'
   import asyncio
   from google.adk.sessions import VertexAiSessionService
   svc = VertexAiSessionService(project="PROJECT_ID", location="us-central1", agent_engine_id="SESSIONS_ENGINE_ID")
   async def go():
       s = await svc.create_session(app_name="trend_scout", user_id="p3-probe@example.com")
       print("ok", s.id, s.user_id)
       await svc.delete_session(app_name="trend_scout", user_id=s.user_id, session_id=s.id)
   asyncio.run(go())
   EOF
   ```
   Expected: `ok <id> p3-probe@example.com`. If it is rejected, change `normalize_user_id`/`normalizeUserId` in Tasks 1 and 4 to map `@` to `_at_` on both sides.

## Task 1: Backend pure authz helpers

**Files:** Create `runserver/authz.py`, `tests/test_authz.py`.

**Step 1: Write the failing test** (`tests/test_authz.py`):
```python
"""P3 per-user authorization helpers (offline, no creds)."""

from __future__ import annotations

import pytest

from runserver.authz import (
    AuthzMode,
    UserAuthzError,
    authorize_body_user,
    decide,
    normalize_user_id,
    path_user_id,
    resolve_mode,
)

A = "alice@example.com"


def test_normalize_strips_iap_prefix_and_lowercases():
    assert (
        normalize_user_id(" accounts.google.com:Alice@JordanTotten.Altostrat.com ") == A
    )
    with pytest.raises(ValueError):
        normalize_user_id("user_1727600000000")


@pytest.mark.parametrize(
    ("path", "user"),
    [
        ("/apps/trend_scout/users/u1/sessions", "u1"),
        ("/apps/trend_scout/users/u1/sessions/123/artifacts/a/b.png", "u1"),
        ("/runs/trend_scout/u1/123", "u1"),
        ("/runs/trend_scout/u1/123/resume", "u1"),
        ("/runs/trend_scout", None),
        ("/list-apps", None),
    ],
)
def test_path_user_id(path, user):
    assert path_user_id(path) == user


def test_resolve_mode():
    assert resolve_mode({}) is AuthzMode.ENFORCE
    assert resolve_mode({"USER_AUTHZ_MODE": "observe"}) is AuthzMode.OBSERVE
    assert resolve_mode({"TRUST_CLIENT_USER_ID": "1"}) is AuthzMode.TRUST_CLIENT
    with pytest.raises(RuntimeError, match="local-dev only"):
        resolve_mode({"TRUST_CLIENT_USER_ID": "1", "K_SERVICE": "trend-trawler-api"})
    with pytest.raises(ValueError):
        resolve_mode({"USER_AUTHZ_MODE": "enforse"})


def test_decide_enforce():
    E = AuthzMode.ENFORCE
    assert decide(E, "/list-apps", None) is None
    assert decide(E, f"/runs/x/{A}/1", A) is None
    assert decide(E, "/runs/x/bob@example.com/1", A)[0] == 403
    assert decide(E, f"/runs/x/{A}/1", None)[0] == 401
    assert decide(E, "/run_sse", A)[0] == 404
    assert decide(E, f"/apps/x/users/{A}/memory", A)[0] == 404


def test_decide_observe_and_trust_client_never_block():
    for mode in (AuthzMode.OBSERVE, AuthzMode.TRUST_CLIENT):
        assert decide(mode, "/runs/x/bob@x.com/1", A) is None
        assert decide(mode, "/run_sse", None) is None


def test_authorize_body_user():
    assert authorize_body_user(AuthzMode.TRUST_CLIENT, None, "me") == "me"
    assert authorize_body_user(AuthzMode.OBSERVE, A, "me") == "me"
    assert authorize_body_user(AuthzMode.ENFORCE, A, A) == A
    with pytest.raises(UserAuthzError) as e:
        authorize_body_user(AuthzMode.ENFORCE, A, "bob@x.com")
    assert e.value.status == 403
    with pytest.raises(UserAuthzError) as e:
        authorize_body_user(AuthzMode.ENFORCE, None, A)
    assert e.value.status == 401
```

**Step 2:** `uv run pytest tests/test_authz.py -v`. Expected: FAIL (`ModuleNotFoundError: runserver.authz`).

**Step 3: Minimal implementation** (`runserver/authz.py`):
```python
"""P3 per-user authorization: the backend half of the proxy-authoritative design.

The /api/adk proxy verifies the IAP JWT and sends the normalized email as
``X-TT-User``. This module trusts that header only from the proxy SA (see
``verify_proxy_caller``) and rejects any path/body ``userId`` that differs."""

from __future__ import annotations

import enum
import logging
import os
import re
from collections.abc import Mapping

TRUSTED_USER_HEADER = "x-tt-user"
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
_PATH_USER_RES = (
    re.compile(r"^/apps/[^/]+/users/(?P<user>[^/]+)(?:/|$)"),
    re.compile(r"^/runs/[^/]+/(?P<user>[^/]+)/[^/]+(?:/resume)?/?$"),
)
# Canned routes the frontend never calls; they bypass /runs or mutate user data.
_BLOCKED_RE = re.compile(
    r"^/(?:run|run_sse|run_live)/?$|^/apps/[^/]+/users/[^/]+/memory/?$"
)
log = logging.getLogger(__name__)


class AuthzMode(enum.StrEnum):
    TRUST_CLIENT = "trust_client"
    OBSERVE = "observe"
    ENFORCE = "enforce"


class UserAuthzError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def resolve_mode(env: Mapping[str, str] = os.environ) -> AuthzMode:
    if env.get("TRUST_CLIENT_USER_ID") == "1":
        if env.get("K_SERVICE"):
            raise RuntimeError(
                "TRUST_CLIENT_USER_ID=1 is local-dev only; refusing on Cloud Run"
            )
        return AuthzMode.TRUST_CLIENT
    return AuthzMode(env.get("USER_AUTHZ_MODE", "enforce").strip().lower())


def normalize_user_id(raw: str) -> str:
    value = raw.strip().lower().removeprefix("accounts.google.com:")
    if not _EMAIL_RE.fullmatch(value):
        raise ValueError(f"not a valid user email: {raw!r}")
    return value


def path_user_id(path: str) -> str | None:
    for rx in _PATH_USER_RES:
        if m := rx.match(path):
            return m.group("user")
    return None


def decide(mode: AuthzMode, path: str, trusted: str | None) -> tuple[int, str] | None:
    """Return ``(status, detail)`` to deny, or ``None`` to allow."""
    if mode is AuthzMode.TRUST_CLIENT:
        return None
    denial: tuple[int, str] | None = None
    if _BLOCKED_RE.match(path):
        denial = (404, "Not found")
    elif (claimed := path_user_id(path)) is not None:
        if trusted is None:
            denial = (401, "missing trusted user identity")
        elif claimed != trusted:
            denial = (403, "user mismatch")
    if denial and mode is AuthzMode.OBSERVE:
        log.warning(
            "authz observe: would deny %s %s (trusted=%s)", denial[0], path, trusted
        )
        return None
    return denial


def authorize_body_user(mode: AuthzMode, trusted: str | None, claimed: str) -> str:
    if mode is AuthzMode.TRUST_CLIENT:
        return claimed
    if mode is AuthzMode.OBSERVE:
        if trusted != claimed:
            log.warning("authz observe: body userId %s != trusted %s", claimed, trusted)
        return claimed
    if trusted is None:
        raise UserAuthzError(401, "missing trusted user identity")
    if claimed != trusted:
        raise UserAuthzError(403, "user mismatch")
    return trusted
```

**Step 4:** `uv run pytest tests/test_authz.py -v && uv run ruff check runserver tests && uv run ty check`. Expected: PASS.

**Step 5:** `git add runserver/authz.py tests/test_authz.py && git commit -m "feat(runserver): add per-user authz helpers (mode, normalization, route decisions)"`

## Task 2: Backend proxy-caller token verification

**Files:** Modify `runserver/authz.py`, `tests/test_authz.py`. Run `uv add google-auth` so the direct import is declared (it is already a transitive dependency via `google-adk`). Then regenerate the root `requirements.txt` with the `uv export …` command in its header; CI checks for drift.

**Step 1: Failing test** (append):
```python
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt
from google.auth import jwt as gjwt

from runserver.authz import verify_proxy_caller

SA = "tt-web-sa@PROJECT_ID.iam.gserviceaccount.com"
AUD = "$API"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUB = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.PKCS1
)
_SIGNER = crypt.RSASigner.from_string(
    _KEY.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ),
    key_id="k1",
)


def _tok(**over) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUD,
        "email": SA,
        "email_verified": True,
        "iat": now,
        "exp": now + 300,
    } | over
    return "Bearer " + gjwt.encode(_SIGNER, claims).decode()


def _ok(auth):
    return verify_proxy_caller(
        auth, audiences=[AUD], trusted_sa=SA, certs=lambda: {"k1": _PUB}
    )


def test_verify_proxy_caller():
    assert _ok(_tok())
    assert not _ok(_tok(email="someone@PROJECT_ID.iam.gserviceaccount.com"))
    assert not _ok(_tok(aud="https://evil.example"))
    assert not _ok(_tok(exp=int(time.time()) - 600))
    assert not _ok(None) and not _ok("Basic abc") and not _ok("Bearer not-a-jwt")
```

**Step 2:** `uv run pytest tests/test_authz.py::test_verify_proxy_caller -v`. Expected: FAIL (ImportError).

**Step 3: Implement** (append to `runserver/authz.py`):
```python
import json
import time
import urllib.request
from collections.abc import Callable, Iterable

import google.auth.exceptions
from google.auth import jwt as google_jwt

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
_certs: dict[str, str] = {}
_certs_at = float("-inf")


def google_certs(ttl: float = 3600.0) -> dict[str, str]:
    """Google OAuth2 signing certs (PEM by kid), cached ``ttl`` seconds."""
    global _certs, _certs_at
    if time.monotonic() - _certs_at > ttl:
        with urllib.request.urlopen(GOOGLE_CERTS_URL, timeout=5) as resp:
            _certs = json.load(resp)
        _certs_at = time.monotonic()
    return _certs


def verify_proxy_caller(
    authorization: str | None,
    *,
    audiences: Iterable[str],
    trusted_sa: str,
    certs: Callable[[], Mapping[str, str | bytes]] = google_certs,
) -> bool:
    """True iff ``authorization`` is a valid Google ID token minted for ``trusted_sa``."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    try:
        claims = google_jwt.decode(
            authorization[7:].strip(), certs=dict(certs()), audience=list(audiences)
        )
    except (ValueError, google.auth.exceptions.GoogleAuthError):
        return False
    return (
        claims.get("iss") in ("https://accounts.google.com", "accounts.google.com")
        and claims.get("email") == trusted_sa
        and claims.get("email_verified") is True
    )
```

**Step 4:** `uv run pytest tests/test_authz.py -v && uv run ty check`. Expected: PASS.

**Step 5:** `git add runserver/authz.py tests/test_authz.py pyproject.toml uv.lock requirements.txt && git commit -m "feat(runserver): verify the proxy service-account ID token before trusting X-TT-User"`

## Task 3: Middleware, ownership 404, router body check, wiring

**Files:** Modify `runserver/authz.py`, `runserver/async_runs.py:647-711` (`configure`, `http_start_run`), `deployment/async_app.py:94-97`. Test in `tests/test_authz.py` and `tests/test_async_runs.py`.

**Step 1: Failing tests.** Append to `tests/test_authz.py`:
```python
import asyncio

import httpx
from fastapi import FastAPI

from runserver.authz import UserAuthzMiddleware, install_ownership_handler


def _app(mode):
    app = FastAPI()

    @app.get("/apps/{a}/users/{u}/sessions/{s}")
    async def get(a: str, u: str, s: str):
        if s == "foreign":
            raise ValueError(f"Session {s} does not belong to user {u}.")
        return {"user": u}

    install_ownership_handler(app)
    app.add_middleware(
        UserAuthzMiddleware, mode=mode, caller_ok=lambda auth: auth == "Bearer proxy"
    )
    return app


def _get(app, path, **headers):
    async def go():
        t = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
            return await c.get(path, headers=headers)

    return asyncio.run(go())


def test_middleware_enforce():
    app, ok = _app(AuthzMode.ENFORCE), {"authorization": "Bearer proxy", "x-tt-user": A}
    assert _get(app, f"/apps/x/users/{A}/sessions/1", **ok).status_code == 200
    assert _get(app, "/apps/x/users/bob@x.com/sessions/1", **ok).status_code == 403
    # the artifacts listing route (7 segments) is user-scoped too
    assert (
        _get(app, "/apps/x/users/bob@x.com/sessions/1/artifacts", **ok).status_code
        == 403
    )
    # X-TT-User from a non-proxy caller is ignored -> no trusted identity -> 401
    assert (
        _get(
            app,
            f"/apps/x/users/{A}/sessions/1",
            authorization="Bearer other",
            **{"x-tt-user": A},
        ).status_code
        == 401
    )
    assert _get(app, f"/apps/x/users/{A}/sessions/foreign", **ok).status_code == 404


def test_middleware_trust_client_passes_everything():
    assert (
        _get(_app(AuthzMode.TRUST_CLIENT), "/apps/x/users/me/sessions/1").status_code
        == 200
    )
```
Append to `tests/test_async_runs.py` (next to `test_router_maps_run_already_active_to_409_on_start_and_resume`):
```python
def test_router_start_run_enforces_body_user_id():
    import httpx
    from fastapi import FastAPI

    from runserver.authz import AuthzMode, UserAuthzMiddleware

    me = "alice@example.com"

    async def _go():
        svc = InMemorySessionService()
        gate = asyncio.Event()
        gate.set()
        runner = _GatedRunner(svc, "creative_agent", gate)
        async_runs.configure(
            session_service=svc,
            runner_factory=lambda a: runner,
            authz_mode=AuthzMode.ENFORCE,
        )
        app = FastAPI()
        app.include_router(router)
        app.add_middleware(
            UserAuthzMiddleware,
            mode=AuthzMode.ENFORCE,
            caller_ok=lambda auth: auth == "Bearer proxy",
        )
        hdrs = {"authorization": "Bearer proxy", "x-tt-user": me}
        t = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
            bad = await c.post(
                "/runs/creative_agent",
                headers=hdrs,
                json={"userId": "bob@x.com", "sessionId": "s", "message": "hi"},
            )
            anon = await c.post(
                "/runs/creative_agent",
                json={"userId": me, "sessionId": "s", "message": "hi"},
            )
            good = await c.post(
                "/runs/creative_agent",
                headers=hdrs,
                json={"userId": me, "sessionId": "s", "message": "hi"},
            )
        for task in list(async_runs._ACTIVE_RUNS.values()):
            if isinstance(task, asyncio.Task):
                await task
        return bad, anon, good

    try:
        bad, anon, good = asyncio.run(_go())
    finally:
        async_runs.configure(session_service=None, runner_factory=None)
    assert bad.status_code == 403
    assert anon.status_code == 401
    assert good.status_code == 200
```

**Step 2:** `uv run pytest tests/test_authz.py tests/test_async_runs.py -k "middleware or enforces_body" -v`. Expected: FAIL (ImportError / unexpected kwarg `authz_mode`).

**Step 3: Implement.** Append to `runserver/authz.py`:
```python
from starlette.requests import Request
from starlette.responses import JSONResponse


class UserAuthzMiddleware:
    """Pure-ASGI: derive the trusted user, stash it on scope state, deny per ``decide``."""

    def __init__(
        self, app, *, mode: AuthzMode, caller_ok: Callable[[str | None], bool]
    ):
        self.app, self.mode, self.caller_ok = app, mode, caller_ok

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] not in ("http", "websocket")
            or self.mode is AuthzMode.TRUST_CLIENT
        ):
            return await self.app(scope, receive, send)
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope["headers"]
        }
        trusted = None
        if (claimed := headers.get(TRUSTED_USER_HEADER)) and self.caller_ok(
            headers.get("authorization")
        ):
            try:
                trusted = normalize_user_id(claimed)
            except ValueError:
                trusted = None
        scope.setdefault("state", {})["tt_user"] = trusted
        denial = decide(self.mode, scope["path"], trusted)
        if denial is None:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        await JSONResponse({"detail": denial[1]}, status_code=denial[0])(
            scope, receive, send
        )


def trusted_user(request: Request) -> str | None:
    return getattr(request.state, "tt_user", None)


def install_ownership_handler(app) -> None:
    """VertexAiSessionService raises ValueError('... does not belong to user ...') on
    an ownership mismatch; the canned routes would 500. Map it to 404 (don't leak existence)."""

    async def _handler(request: Request, exc: Exception):
        if "does not belong to user" in str(exc):
            return JSONResponse({"detail": "Session not found"}, status_code=404)
        raise exc

    app.add_exception_handler(ValueError, _handler)
```
In `runserver/async_runs.py`:
- Add `from fastapi import Request` and `from runserver.authz import AuthzMode, UserAuthzError, authorize_body_user, trusted_user`.
- Add module global `_AUTHZ_MODE = AuthzMode.TRUST_CLIENT`. `configure(*, session_service, runner_factory, authz_mode: AuthzMode = AuthzMode.TRUST_CLIENT)` sets it. The default keeps every existing test on today's behavior.
- In `http_start_run(app_name: str, body: _StartRunBody, request: Request)`, before `start_run`:
  ```python
  try:
      user_id = authorize_body_user(_AUTHZ_MODE, trusted_user(request), body.userId)
  except UserAuthzError as exc:
      raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
  ```
  and pass `user_id=user_id`. The path-based `GET` and `resume` routes are covered by the middleware.

In `deployment/async_app.py`, replace the final two lines with:
```python
_AUTHZ_MODE = resolve_mode()
_PROXY_AUDIENCES = [a for a in os.getenv("TRUSTED_PROXY_AUDIENCES", "").split(",") if a]
_PROXY_SA = os.getenv("TRUSTED_PROXY_SA", "")
if _AUTHZ_MODE is AuthzMode.ENFORCE and not (_PROXY_AUDIENCES and _PROXY_SA):
    raise RuntimeError(
        "USER_AUTHZ_MODE=enforce needs TRUSTED_PROXY_SA + TRUSTED_PROXY_AUDIENCES"
    )

configure(
    session_service=session_service,
    runner_factory=_runner_factory,
    authz_mode=_AUTHZ_MODE,
)
app.include_router(router)
install_ownership_handler(app)
app.add_middleware(
    UserAuthzMiddleware,
    mode=_AUTHZ_MODE,
    caller_ok=partial(
        verify_proxy_caller, audiences=_PROXY_AUDIENCES, trusted_sa=_PROXY_SA
    ),
)
```
Add the imports `from functools import partial` and `from runserver.authz import AuthzMode, UserAuthzMiddleware, install_ownership_handler, resolve_mode, verify_proxy_caller`. Update the `CLAUDE.md` local-dev command in Task 7 to `TRUST_CLIENT_USER_ID=1 ALLOW_ORIGINS=… uv run uvicorn deployment.async_app:app --port 8000`.

**Step 4:** `uv run pytest tests/ -v && uv run ruff check . && uv run ruff format --check && uv run ty check`. Expected: all PASS. Also run `TRUST_CLIENT_USER_ID=1 uv run uvicorn deployment.async_app:app --port 8000` and `curl localhost:8000/list-apps` to confirm it boots and returns the 3 apps.

**Step 5:** `git add runserver deployment/async_app.py tests && git commit -m "feat(api): enforce trusted per-user identity on /runs and canned session routes"`

## Task 4: Proxy IAP JWT verification

**Files:** Create `frontend/src/lib/iap-identity.ts`, `frontend/src/__tests__/iap-identity.test.ts`. Run `cd frontend && npm install jose` (it is already present transitively at 6.2.x; this makes it a direct dependency).

**Step 1: Failing test:**
```ts
// @vitest-environment node
import { describe, it, expect } from "vitest";
import { generateKeyPair, SignJWT, exportJWK, createLocalJWKSet } from "jose";
import { normalizeUserId, verifyIapJwt, resolveUser, IAP_ISSUER } from "@/lib/iap-identity";

const AUD = "/projects/PROJECT_NUMBER/locations/us-central1/services/trend-trawler-web";

async function setup() {
  const { publicKey, privateKey } = await generateKeyPair("ES256");
  const keys = createLocalJWKSet({ keys: [{ ...(await exportJWK(publicKey)), kid: "k1", alg: "ES256" }] });
  const sign = (claims: Record<string, unknown>, aud = AUD, iss = IAP_ISSUER) =>
    new SignJWT(claims).setProtectedHeader({ alg: "ES256", kid: "k1" }).setIssuer(iss)
      .setAudience(aud).setIssuedAt().setExpirationTime("5m").sign(privateKey);
  return { keys, sign };
}

describe("normalizeUserId", () => {
  it("strips the IAP prefix and lowercases", () => {
    expect(normalizeUserId("accounts.google.com:Alice@X.com")).toBe("alice@x.com");
    expect(() => normalizeUserId("user_1727600000000")).toThrow();
  });
});

describe("verifyIapJwt", () => {
  it("returns the normalized email for a valid assertion", async () => {
    const { keys, sign } = await setup();
    expect(await verifyIapJwt(await sign({ email: "Alice@X.com" }), { audience: AUD, keys })).toBe("alice@x.com");
  });
  it("rejects wrong audience or issuer", async () => {
    const { keys, sign } = await setup();
    await expect(verifyIapJwt(await sign({ email: "a@x.com" }, "/projects/1/global/backendServices/2"), { audience: AUD, keys })).rejects.toThrow();
    await expect(verifyIapJwt(await sign({ email: "a@x.com" }, AUD, "https://evil"), { audience: AUD, keys })).rejects.toThrow();
  });
});

describe("resolveUser", () => {
  const ok = async () => "alice@x.com";
  it("rejects a missing assertion on Cloud Run", async () => {
    expect(await resolveUser(new Headers(), { onCloudRun: true, verify: ok })).toEqual({ kind: "reject", status: 401 });
  });
  it("passes through locally with no assertion", async () => {
    expect(await resolveUser(new Headers(), { onCloudRun: false, verify: ok })).toEqual({ kind: "local" });
  });
  it("ignores the spoofable email header, uses the JWT", async () => {
    const h = new Headers({ "x-goog-iap-jwt-assertion": "jwt", "x-goog-authenticated-user-email": "accounts.google.com:bob@x.com" });
    expect(await resolveUser(h, { onCloudRun: true, verify: ok })).toEqual({ kind: "user", userId: "alice@x.com" });
  });
  it("rejects an invalid assertion", async () => {
    const bad = async () => { throw new Error("bad sig"); };
    const h = new Headers({ "x-goog-iap-jwt-assertion": "jwt" });
    expect(await resolveUser(h, { onCloudRun: true, verify: bad })).toEqual({ kind: "reject", status: 401 });
  });
});
```

**Step 2:** `cd frontend && npx vitest run src/__tests__/iap-identity.test.ts`. Expected: FAIL (module not found).

**Step 3: Implement** `frontend/src/lib/iap-identity.ts`:
```ts
import { createRemoteJWKSet, jwtVerify, type JWTVerifyGetKey } from "jose";

export const IAP_ISSUER = "https://cloud.google.com/iap";
const IAP_JWKS = createRemoteJWKSet(new URL("https://www.gstatic.com/iap/verify/public_key-jwk"));
const EMAIL_RE = /^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$/;
const MD = "http://metadata.google.internal/computeMetadata/v1";

/** Must match runserver.authz.normalize_user_id exactly. */
export function normalizeUserId(raw: string): string {
  const v = raw.trim().toLowerCase().replace(/^accounts\.google\.com:/, "");
  if (!EMAIL_RE.test(v)) throw new Error(`not a valid user email: ${raw}`);
  return v;
}

export async function verifyIapJwt(
  token: string, opts: { audience: string; keys?: JWTVerifyGetKey },
): Promise<string> {
  const { payload } = await jwtVerify(token, opts.keys ?? IAP_JWKS, {
    issuer: IAP_ISSUER, audience: opts.audience, algorithms: ["ES256"], clockTolerance: 30,
  });
  if (typeof payload.email !== "string") throw new Error("IAP JWT has no email");
  return normalizeUserId(payload.email);
}

let audiencePromise: Promise<string> | undefined;
/** `/projects/N/locations/REGION/services/K_SERVICE` from the metadata server
 *  (no env vars → web redeploys stay env-flag-free); IAP_AUDIENCE overrides. */
export function iapAudience(): Promise<string> {
  if (process.env.IAP_AUDIENCE) return Promise.resolve(process.env.IAP_AUDIENCE);
  audiencePromise ??= (async () => {
    const get = async (p: string) =>
      (await (await fetch(`${MD}/${p}`, { headers: { "Metadata-Flavor": "Google" } })).text()).trim();
    const num = await get("project/numeric-project-id");
    const region = (await get("instance/region")).split("/").pop(); // projects/N/regions/R
    return `/projects/${num}/locations/${region}/services/${process.env.K_SERVICE}`;
  })().catch((e) => { audiencePromise = undefined; throw e; });
  return audiencePromise;
}

export type ResolvedUser =
  | { kind: "user"; userId: string } | { kind: "local" } | { kind: "reject"; status: 401 };

export async function resolveUser(
  headers: Headers,
  opts: { onCloudRun: boolean; verify?: (token: string) => Promise<string> },
): Promise<ResolvedUser> {
  const token = headers.get("x-goog-iap-jwt-assertion");
  if (!token) return opts.onCloudRun ? { kind: "reject", status: 401 } : { kind: "local" };
  const verify = opts.verify ?? (async (t: string) => verifyIapJwt(t, { audience: await iapAudience() }));
  try {
    return { kind: "user", userId: await verify(token) };
  } catch (err) {
    console.warn("IAP JWT rejected:", (err as Error).message);
    return { kind: "reject", status: 401 };
  }
}
```

**Step 4:** `cd frontend && npx vitest run src/__tests__/iap-identity.test.ts && npx tsc --noEmit && npm run lint`. Expected: PASS.

**Step 5:** `git add frontend/package.json frontend/package-lock.json frontend/src/lib/iap-identity.ts frontend/src/__tests__/iap-identity.test.ts && git commit -m "feat(web): verify the IAP JWT assertion in the /api/adk proxy"`

## Task 5: Proxy route allowlist + userId rewrite

**Files:** Create `frontend/src/lib/user-scoping.ts`, `frontend/src/__tests__/user-scoping.test.ts`.

**Step 1: Failing test:**
```ts
import { describe, it, expect } from "vitest";
import { scopeRequestToUser } from "@/lib/user-scoping";

const U = "alice@x.com";
describe("scopeRequestToUser", () => {
  it("rewrites the path user segment on canned session routes", () => {
    expect(scopeRequestToUser("GET", ["apps", "trend_scout", "users", "me", "sessions", "42"], undefined, U))
      .toEqual({ path: "apps/trend_scout/users/alice%40x.com/sessions/42", body: undefined });
  });
  it("rewrites poll and resume paths", () => {
    expect(scopeRequestToUser("GET", ["runs", "a", "bob@x.com", "42"], undefined, U)?.path).toBe("runs/a/alice%40x.com/42");
    expect(scopeRequestToUser("POST", ["runs", "a", "me", "42", "resume"], "{}", U)?.path).toBe("runs/a/alice%40x.com/42/resume");
  });
  it("overwrites the kick-off body userId", () => {
    const r = scopeRequestToUser("POST", ["runs", "a"], JSON.stringify({ userId: "bob", sessionId: "42", message: "m" }), U);
    expect(JSON.parse(r!.body!)).toEqual({ userId: U, sessionId: "42", message: "m" });
  });
  it("blocks routes the UI never uses", () => {
    expect(scopeRequestToUser("POST", ["run_sse"], "{}", U)).toBeNull();
    expect(scopeRequestToUser("DELETE", ["apps", "a", "users", "me", "sessions", "42"], undefined, U)).toBeNull();
    expect(scopeRequestToUser("PATCH", ["apps", "a", "users", "me", "memory"], "{}", U)).toBeNull();
  });
  it("scopes the artifacts listing and artifact fetch", () => {
    expect(scopeRequestToUser("GET", ["apps", "a", "users", "me", "sessions", "42", "artifacts"], undefined, U)?.path)
      .toBe("apps/a/users/alice%40x.com/sessions/42/artifacts");
    expect(scopeRequestToUser("GET", ["apps", "a", "users", "me", "sessions", "42", "artifacts", "img.png"], undefined, U)).not.toBeNull();
  });
  it("allows list-apps untouched", () => {
    expect(scopeRequestToUser("GET", ["list-apps"], undefined, U)).toEqual({ path: "list-apps", body: undefined });
  });
});
```

**Step 2:** `cd frontend && npx vitest run src/__tests__/user-scoping.test.ts`. Expected: FAIL.

**Step 3: Implement** `frontend/src/lib/user-scoping.ts`:
```ts
/** Routes the UI uses (src/lib/api.ts). `userAt` = index of the user segment to overwrite. */
const ROUTES: { method: string; match: (p: string[]) => boolean; userAt?: number; bodyUser?: true }[] = [
  { method: "GET", match: (p) => p.length === 1 && (p[0] === "list-apps" || p[0] === "health") },
  { method: "POST", match: (p) => p.length === 5 && p[0] === "apps" && p[2] === "users" && p[4] === "sessions", userAt: 3 },
  { method: "GET", match: (p) => p.length >= 5 && p[0] === "apps" && p[2] === "users" && p[4] === "sessions"
      && (p.length <= 6 || p[6] === "artifacts"), userAt: 3 },
  { method: "POST", match: (p) => p.length === 2 && p[0] === "runs", bodyUser: true },
  { method: "GET", match: (p) => p.length === 4 && p[0] === "runs", userAt: 2 },
  { method: "POST", match: (p) => p.length === 5 && p[0] === "runs" && p[4] === "resume", userAt: 2 },
];

/** Returns the scoped upstream path/body, or null if the route isn't allowed. Throws on bad JSON. */
export function scopeRequestToUser(
  method: string, path: string[], body: string | undefined, userId: string,
): { path: string; body: string | undefined } | null {
  const route = ROUTES.find((r) => r.method === method && r.match(path));
  if (!route) return null;
  const segs = [...path];
  if (route.userAt !== undefined) segs[route.userAt] = encodeURIComponent(userId);
  let outBody = body;
  if (route.bodyUser) outBody = JSON.stringify({ ...JSON.parse(body ?? "{}"), userId });
  return { path: segs.join("/"), body: outBody };
}
```

**Step 4:** `cd frontend && npx vitest run src/__tests__/user-scoping.test.ts && npx tsc --noEmit`. Expected: PASS.

**Step 5:** `git add frontend/src/lib/user-scoping.ts frontend/src/__tests__/user-scoping.test.ts && git commit -m "feat(web): allowlist proxied ADK routes and rewrite userId to the caller"`

## Task 6: Wire the proxy + client placeholder

**Files:** Modify `frontend/src/app/api/adk/[...path]/route.ts`, `frontend/src/lib/api.ts`, `frontend/src/app/page.tsx:85`, `frontend/src/app/run/[sessionId]/page.tsx:74`, `frontend/src/app/results/[sessionId]/page.tsx:130`, `frontend/src/__tests__/adk-proxy-auth.test.ts`.

**Step 1: Failing test** (append to `adk-proxy-auth.test.ts`; a browser must not be able to forge the trusted header):
```ts
it("strips a client-supplied x-tt-user so only the proxy can set it", () => {
  const headers = new Headers({ "x-tt-user": "bob@x.com" });
  stripInboundCredentials(headers);
  expect(headers.get("x-tt-user")).toBeNull();
});
```
**Step 2:** `cd frontend && npx vitest run src/__tests__/adk-proxy-auth.test.ts`. Expected: FAIL.

**Step 3: Implement.**
- Add `"x-tt-user"` to `INBOUND_CREDENTIAL_HEADERS`.
- In `proxy()`, *before* `stripInboundCredentials`:
  ```ts
  const who = await resolveUser(request.headers, { onCloudRun: !!process.env.K_SERVICE });
  if (who.kind === "reject") return new Response("Unauthenticated", { status: who.status });
  ```
- After reading `body`:
  ```ts
  let upstreamPath = path.join("/"), upstreamBody = body;
  if (who.kind === "user") {
    let scoped;
    try { scoped = scopeRequestToUser(method, path, body, who.userId); }
    catch { return new Response("Bad JSON body", { status: 400 }); }
    if (!scoped) return new Response("Not found", { status: 404 });
    ({ path: upstreamPath, body: upstreamBody } = scoped);
    headers.set("x-tt-user", who.userId);
  }
  ```
  Build `target` from `upstreamPath` and send `upstreamBody`. Move the `target` construction below this block.
- In `api.ts`, add `export const SELF_USER_ID = "me";` with a doc comment ("placeholder; the proxy substitutes the IAP-verified user").
- In `page.tsx`, use `const userId = SELF_USER_ID;`. In the run and results pages, use `searchParams.get("userId") || SELF_USER_ID`.

**Step 4:** `cd frontend && npm test && npm run lint && npx tsc --noEmit && npm run build`. Expected: all PASS. Local smoke: run the backend with `TRUST_CLIENT_USER_ID=1 …uvicorn…` and the frontend with `npm run dev`, submit a `trend_scout` campaign, and check that the run view polls. No JWT locally means the proxy passes through.

**Step 5:** `git add frontend && git commit -m "feat(web): inject the IAP-verified user into proxied ADK calls"`

## Task 7: Docs

**Files:** `CLAUDE.md` (local-dev command gets `TRUST_CLIENT_USER_ID=1`; Frontend/Deployment paragraph gets the P3 trust model), `deployment/README.md` (new env vars `USER_AUTHZ_MODE`, `TRUSTED_PROXY_SA`, `TRUSTED_PROXY_AUDIENCES`; the "Frontend + api_server" runbook), `tests/README.md` (add `test_authz.py`), `frontend/.env.example` (optional `IAP_AUDIENCE`).
Run `uv run pytest tests/ -q && (cd frontend && npm test)`, then `git commit -m "docs: document per-user authz trust model and env flags"`.

## Task 8: Rollout (backend accepting both, then proxy, then enforcement), live verification, rollback

Merge the PR first. Deploy from a clean `main` worktree. The api deploy's success line has printed the **old** revision before. Find the new revision by newest `creationTimestamp` (`gcloud run revisions list --service trend-trawler-api --region us-central1 --sort-by=~metadata.creationTimestamp --limit 3`).

**Phase A: api in observe mode (accepts both).**
1. Set env vars **without redeploying code**. `--update-env-vars` merges and never replaces:
   `gcloud run services update trend-trawler-api --region us-central1 --update-env-vars USER_AUTHZ_MODE=observe,TRUSTED_PROXY_SA=tt-web-sa@PROJECT_ID.iam.gserviceaccount.com,TRUSTED_PROXY_AUDIENCES=$API`
   Then pin traffic: `gcloud run services update-traffic trend-trawler-api --region us-central1 --to-revisions <new>=100`.
2. Deploy the code with the runbook flags and **no env flag**:
   `gcloud run deploy trend-trawler-api --source . --region us-central1 --no-allow-unauthenticated --service-account tt-api-sa@PROJECT_ID.iam.gserviceaccount.com --memory 8Gi --cpu 4 --min-instances 1 --timeout 900 --no-cpu-throttling`
   Then **always** run `gcloud run services update-traffic trend-trawler-api --region us-central1 --to-revisions <new>=100 --update-tags main-clean=<new>`.
3. Check: unauth `curl -s -o /dev/null -w '%{http_code}' $API/list-apps` gives `403`. The UI still works with the old web. Logs show `authz observe: would deny 401` lines, which is expected because the old proxy sends no `X-TT-User`.

**Phase B: web proxy.** Confirm no run is in flight (legacy `user_<ts>` runs stop resolving at this point).
1. `gcloud run deploy trend-trawler-web --source ./frontend --region us-central1 --service-account tt-web-sa@PROJECT_ID.iam.gserviceaccount.com --no-traffic --tag p3`. Do **not** pass `--allow-unauthenticated` or any `--set-env-vars`/`--update-env-vars`; IAP and `ADK_API_BASE` are preserved.
2. Open `the `p3` tag URL (`gcloud run services describe trend-trawler-web --format="value(status.traffic)"`)`. It is IAP-gated. Run a `trend_scout` campaign end to end. If the proxy returns 401, read the `IAP JWT rejected:` log line. On an audience mismatch, decode a real assertion's `aud` and set `IAP_AUDIENCE` with a one-off `gcloud run services update trend-trawler-web --update-env-vars IAP_AUDIENCE=…`.
3. Api logs should show **no** `would deny` lines for tag traffic. Then promote with `gcloud run services update-traffic trend-trawler-web --region us-central1 --to-latest` to restore auto-routing, and remove the tag with `--remove-tags p3`.

**Phase C: enforce.** `gcloud run services update trend-trawler-api --region us-central1 --update-env-vars USER_AUTHZ_MODE=enforce`, then `update-traffic --to-revisions <new>=100 --update-tags main-clean=<new>`.

**Live verification (user A cannot read user B's session).**
1. Create user B's session straight in the store. Use the Task 0 snippet with `user_id="p3-probe-b@example.com"` and no delete, then note `B_ID`.
2. As user A (admin, browser devtools on the web URL):
   `await fetch('/api/adk/apps/trend_scout/users/p3-probe-b%40example.com/sessions/B_ID').then(r => r.status)` → **404**. The proxy rewrites to A, and the ownership handler maps the mismatch.
   `await fetch('/api/adk/runs/trend_scout/x/B_ID?since=0').then(r => r.json())` → `{status: "not_found", events: []}`.
   `await fetch('/api/adk/runs/trend_scout/x/B_ID/resume', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{"functionCallId":"c","functionName":"review_research","response":{}}'}).then(r => r.status)` → **404**.
   `await fetch('/api/adk/apps/trend_scout/users/me/sessions').then(r => r.json())` → only sessions whose `userId` is A's email.
   `await fetch('/api/adk/run_sse', {method:'POST', body:'{}'}).then(r => r.status)` → **404**.
3. Bypassing the proxy: `TOK=$(gcloud auth print-identity-token --impersonate-service-account=tt-web-sa@PROJECT_ID.iam.gserviceaccount.com --audiences=$API)`.
   `curl -H "Authorization: Bearer $TOK" -H "X-TT-User: admin@example.com" $API/apps/trend_scout/users/p3-probe-b@example.com/sessions/B_ID` → **403**.
   The same request without `X-TT-User` → **401**.
   With a plain user token (`gcloud auth print-identity-token`) → **401/403** from Cloud Run IAM.
4. If a second domain user is available, repeat step 2 with real user B's run URL from user A's browser. Expected 404 / `not_found`.
5. Clean up: delete the `p3-probe-b` session with the SDK.

**Rollback.**
- Fastest (keeps new code): `gcloud run services update trend-trawler-api --region us-central1 --update-env-vars USER_AUTHZ_MODE=observe`, then pin the new revision to 100.
- api code: `gcloud run services update-traffic trend-trawler-api --region us-central1 --to-revisions trend-trawler-api-00050-hlh=100` (the pre-P3 revision from Task 0).
- web: `gcloud run services update-traffic trend-trawler-web --region us-central1 --to-revisions trend-trawler-web-00020-2cv=100`. This pins web; restore with `--to-latest` after the fix.
- Rolling back web alone while api is on `enforce` breaks the UI with 401s. Always move api to `observe` first.
- Never re-add `allUsers` and never disable IAP. Disabling IAP breaks browsers and in-flight resumes.

## Open questions

1. **Email vs `sub` as the user id.** Email is readable, but a renamed or recycled account would inherit or lose sessions. `sub` (`accounts.google.com:1234…`) is stable but opaque.
2. **Legacy sessions:** is it acceptable that pre-P3 `user_<ts>` runs become UI-unreachable, or do we want a time-boxed read-only grace (proxy passes `^user_\d{13}$` through in observe)? That grace would keep today's hole open for legacy data.
3. **Shared runs:** should teammates be able to view each other's results (a domain-wide read of `/results`)? P3 makes them strictly private. Sharing would need an explicit ACL or admin allowlist.
4. Does `user:admin@…` with direct `run.invoker` on web (kept for `gcloud run services proxy`) still pass through IAP and get an assertion? If it doesn't, that path gets 401 from the proxy after Phase B. Check during Phase B.
5. **Is Agent Engine's `user_id` charset/length OK for emails?** Task 0 answers this.
6. Should the proxy also require `hd == example.com` in the JWT as belt-and-braces over the IAP access policy?
7. P3b: scope `/api/gcs` to the caller's own artifacts. It is out of scope here.
