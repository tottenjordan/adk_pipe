"""P3 per-user authorization: the backend half of the proxy-authoritative design.

The /api/adk proxy verifies the IAP JWT and sends the normalized email as
``X-TT-User``. This module trusts that header only from the proxy SA (see
``verify_proxy_caller``) and rejects any path/body ``userId`` that differs."""

from __future__ import annotations

import asyncio
import base64
import enum
import json
import logging
import os
import re
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping

import google.auth.exceptions
from google.auth import jwt as google_jwt
from starlette.requests import Request
from starlette.responses import JSONResponse

TRUSTED_USER_HEADER = "x-tt-user"
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
_PATH_USER_RES = (
    re.compile(r"^/apps/[^/]+/users/(?P<user>[^/]+)(?:/|$)"),
    re.compile(r"^/runs/[^/]+/(?P<user>[^/]+)/[^/]+(?:/resume)?/?$"),
    # Bandit experiments: GET list/detail/metrics, POST traffic/stop. The bare
    # POST /experiments is body-scoped (authorize_body_user in the handler).
    re.compile(r"^/experiments/(?P<user>[^/]+)(?:/.*)?$"),
    # Human creative ratings (runserver/ratings.py): PUT/GET per session + calibration.
    re.compile(r"^/ratings/(?P<user>[^/]+)(?:/.*)?$"),
    # Shareable creative links (runserver/shares.py): POST per session, GET list,
    # DELETE per token.
    re.compile(r"^/shares/(?P<user>[^/]+)(?:/.*)?$"),
    # Person-reference consent registry (runserver/person_refs.py): GET list,
    # POST register, DELETE per consent_id.
    re.compile(r"^/person-refs/(?P<user>[^/]+)(?:/.*)?$"),
    # Pre-rewrite form: ADK's _DefaultAppRewriteMiddleware (ADK_DEFAULT_APP_NAME)
    # maps /users/... -> /apps/<default>/users/... *after* this middleware runs.
    re.compile(r"^/users/(?P<user>[^/]+)(?:/|$)"),
)
# Canned routes the frontend never calls; they bypass /runs or mutate user data.
_BLOCKED_RE = re.compile(
    r"^/(?:run|run_sse|run_live)/?$"
    r"|^/apps/[^/]+/users/[^/]+/memory/?$"
    r"|^/users/[^/]+/memory/?$"
    r"|^/agent-identity/finalize/?$"
)
# The exact VertexAiSessionService ownership-mismatch message (get/delete_session).
_OWNERSHIP_RE = re.compile(r"Session \S+ does not belong to user .+\.")
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
CERTS_TTL = 3600.0
# After a failed fetch, don't retry for this long (requests fail closed meanwhile);
# also the minimum interval between unknown-kid refreshes.
CERTS_BACKOFF = 60.0
_certs: dict[str, str] = {}
_certs_at = float("-inf")
_certs_failed_at = float("-inf")
_certs_lock = threading.Lock()
_last_reject_log = float("-inf")
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
    if _BLOCKED_RE.match(path):
        # Blocked in observe too: the frontend never calls these routes.
        return (404, "Not found")
    denial: tuple[int, str] | None = None
    if (claimed := path_user_id(path)) is not None:
        if trusted is None:
            denial = (401, "missing trusted user identity")
        elif claimed != trusted:
            denial = (403, "user mismatch")
    if denial and mode is AuthzMode.OBSERVE:
        log.warning(
            "authz observe: would deny %s %r (trusted=%r)", denial[0], path, trusted
        )
        return None
    return denial


def authorize_body_user(mode: AuthzMode, trusted: str | None, claimed: str) -> str:
    if mode is AuthzMode.TRUST_CLIENT:
        return claimed
    if mode is AuthzMode.OBSERVE:
        if trusted != claimed:
            log.warning("authz observe: body userId %r != trusted %r", claimed, trusted)
        return claimed
    if trusted is None:
        raise UserAuthzError(401, "missing trusted user identity")
    if claimed != trusted:
        raise UserAuthzError(403, "user mismatch")
    return trusted


def google_certs(*, refresh: bool = False) -> dict[str, str]:
    """Google OAuth2 signing certs (PEM by kid), cached ``CERTS_TTL`` seconds.

    Blocking (call off the event loop). ``refresh=True`` (unknown kid) forces a
    refetch at most once per ``CERTS_BACKOFF``. A failed fetch is negatively
    cached for ``CERTS_BACKOFF``: with no fresh certs this raises ``OSError``
    (fail closed) without touching the network; otherwise the cache is served."""
    global _certs, _certs_at, _certs_failed_at
    with _certs_lock:
        now = time.monotonic()
        stale = now - _certs_at > CERTS_TTL
        if not (stale or (refresh and now - _certs_at > CERTS_BACKOFF)):
            return _certs
        if now - _certs_failed_at < CERTS_BACKOFF:
            if stale:
                raise OSError("Google certs fetch backing off after a failure")
            return _certs
        try:
            with urllib.request.urlopen(GOOGLE_CERTS_URL, timeout=5) as resp:
                fetched = json.load(resp)
        except (OSError, ValueError) as exc:
            _certs_failed_at = now
            if stale:
                raise OSError("could not fetch Google certs") from exc
            return _certs
        _certs, _certs_at = fetched, time.monotonic()
        return _certs


def _token_kid(token: str) -> str | None:
    """The unverified JWT header ``kid`` (only used to pick/refresh certs)."""
    try:
        seg = token.split(".", 1)[0]
        header = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        kid = header.get("kid")
    except (ValueError, AttributeError):
        return None
    return kid if isinstance(kid, str) else None


def _log_rejection(reason: str) -> None:
    """Rate-limited (once per 60 s) rejection log; never includes the token."""
    global _last_reject_log
    now = time.monotonic()
    if now - _last_reject_log >= 60.0:
        _last_reject_log = now
        log.info("proxy token rejected: %s", reason)


def verify_proxy_caller(
    authorization: str | None,
    *,
    audiences: Iterable[str],
    trusted_sa: str,
    certs: Callable[..., Mapping[str, str | bytes]] = google_certs,
) -> bool:
    """True iff ``authorization`` is a valid Google ID token minted for ``trusted_sa``.

    Blocking (may fetch certs): the middleware runs it in a worker thread.
    ``certs`` is called as ``certs()`` and, for an unknown kid, ``certs(refresh=True)``."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    token = authorization[7:].strip()
    try:
        keys = certs()
        if (kid := _token_kid(token)) is not None and kid not in keys:
            keys = certs(refresh=True)
        claims = google_jwt.decode(
            token,
            certs=dict(keys),
            audience=list(audiences),
            clock_skew_in_seconds=30,
        )
    except (ValueError, OSError, google.auth.exceptions.GoogleAuthError) as exc:
        # Bad token, or certs unreachable (network/backoff): fail closed, not 500.
        _log_rejection(type(exc).__name__)
        return False
    ok = (
        claims.get("iss") in ("https://accounts.google.com", "accounts.google.com")
        and claims.get("email") == trusted_sa
        and claims.get("email_verified") is True
    )
    if not ok:
        _log_rejection("claims mismatch")
    return ok


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
        # Verification may block on a certs fetch: keep it off the event loop
        # shared with the detached /runs tasks.
        if (claimed := headers.get(TRUSTED_USER_HEADER)) and await asyncio.to_thread(
            self.caller_ok, headers.get("authorization")
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
            # Consume the handshake, then refuse it (policy violation); the app
            # never sees the connection.
            if (await receive())["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": 1008})
            return
        await JSONResponse({"detail": denial[1]}, status_code=denial[0])(
            scope, receive, send
        )


def trusted_user(request: Request) -> str | None:
    return getattr(request.state, "tt_user", None)


def install_ownership_handler(app) -> None:
    """VertexAiSessionService raises ValueError('Session X does not belong to user
    Y.') on an ownership mismatch; the canned routes (and /runs start/resume) would
    500. Map exactly that to 404 (don't leak existence); re-raise anything else."""

    async def _handler(request: Request, exc: Exception):
        if type(exc) is ValueError and _OWNERSHIP_RE.fullmatch(str(exc)):
            return JSONResponse({"detail": "Session not found"}, status_code=404)
        raise exc

    app.add_exception_handler(ValueError, _handler)
