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
    r"^/(?:run|run_sse|run_live)/?$"
    r"|^/apps/[^/]+/users/[^/]+/memory/?$"
    r"|^/agent-identity/finalize/?$"
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
