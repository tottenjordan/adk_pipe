"""P3 per-user authorization: the backend half of the proxy-authoritative design.

The /api/adk proxy verifies the IAP JWT and sends the normalized email as
``X-TT-User``. This module trusts that header only from the proxy SA (see
``verify_proxy_caller``) and rejects any path/body ``userId`` that differs."""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping

import google.auth.exceptions
from google.auth import jwt as google_jwt

TRUSTED_USER_HEADER = "x-tt-user"
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
_PATH_USER_RES = (
    re.compile(r"^/apps/[^/]+/users/(?P<user>[^/]+)(?:/|$)"),
    re.compile(r"^/runs/[^/]+/(?P<user>[^/]+)/[^/]+(?:/resume)?/?$"),
)
# Canned routes the frontend never calls; they bypass /runs or mutate user data.
_BLOCKED_RE = re.compile(
    r"^/(?:run|run_sse|run_live)/?$"
    r"|^/apps/[^/]+/users/[^/]+/memory/?$"
    r"|^/agent-identity/finalize/?$"
)
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
_certs: dict[str, str] = {}
_certs_at = float("-inf")
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
    except OSError:
        # Certs fetch failed (network): fail closed rather than 500.
        log.warning("authz: could not fetch Google certs", exc_info=True)
        return False
    return (
        claims.get("iss") in ("https://accounts.google.com", "accounts.google.com")
        and claims.get("email") == trusted_sa
        and claims.get("email_verified") is True
    )
