"""P3 per-user authorization helpers (offline, no creds)."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from google.auth import crypt
from google.auth import jwt as gjwt

from runserver.authz import (
    AuthzMode,
    UserAuthzError,
    UserAuthzMiddleware,
    authorize_body_user,
    decide,
    install_ownership_handler,
    normalize_user_id,
    path_user_id,
    resolve_mode,
    verify_proxy_caller,
)

A = "alice@example.com"


def test_normalize_strips_iap_prefix_and_lowercases():
    assert (
        normalize_user_id(" accounts.google.com:Alice@JordanTotten.Altostrat.com ")
        == "alice@jordantotten.altostrat.com"
    )
    assert normalize_user_id("accounts.google.com:Alice@Example.com") == A
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
        # pre-rewrite form (ADK _DefaultAppRewriteMiddleware adds /apps/<default>)
        ("/users/u1/sessions", "u1"),
        ("/users/u1", "u1"),
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
    # pre-rewrite forms of the same routes (ADK_DEFAULT_APP_NAME set)
    assert decide(E, f"/users/{A}/memory", A)[0] == 404
    assert decide(E, "/users/bob@example.com/sessions", A)[0] == 403
    assert decide(E, f"/users/{A}/sessions", None)[0] == 401
    assert decide(E, f"/users/{A}/sessions", A) is None


@pytest.mark.parametrize(
    "path", ["/agent-identity/finalize", "/agent-identity/finalize/"]
)
def test_decide_blocks_agent_identity_finalize(path):
    assert decide(AuthzMode.ENFORCE, path, A) == (404, "Not found")


def test_decide_observe_blocks_only_blocked_routes_and_trust_client_never_blocks():
    for mode in (AuthzMode.OBSERVE, AuthzMode.TRUST_CLIENT):
        assert decide(mode, "/runs/x/bob@x.com/1", A) is None
        assert decide(mode, "/runs/x/bob@x.com/1", None) is None
    # blocked canned routes are denied in observe too (nothing calls them)
    assert decide(AuthzMode.OBSERVE, "/run_sse", None) == (404, "Not found")
    assert decide(AuthzMode.OBSERVE, f"/users/{A}/memory", A) == (404, "Not found")
    assert decide(AuthzMode.TRUST_CLIENT, "/run_sse", None) is None


def test_observe_logs_user_ids_with_repr(caplog):
    evil = "bob@x.com\nFAKE LOG LINE"
    with caplog.at_level("WARNING", logger="runserver.authz"):
        decide(AuthzMode.OBSERVE, "/runs/x/bob@x.com/1", evil)
        authorize_body_user(AuthzMode.OBSERVE, A, evil)
    text = caplog.text
    assert "\nFAKE" not in text and "\\nFAKE" in text


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


SA = "tt-web-sa@test-project.iam.gserviceaccount.com"
AUD = "https://trend-trawler-api.example.run.app"


def _signer(key, kid="k1"):
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return crypt.RSASigner.from_string(pem, key_id=kid)


_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUB = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.PKCS1
)
_SIGNER = _signer(_KEY)
# A different key presenting the trusted kid: a forged/bad signature.
_EVIL_SIGNER = _signer(rsa.generate_private_key(public_exponent=65537, key_size=2048))


def _tok(signer=_SIGNER, **over) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUD,
        "email": SA,
        "email_verified": True,
        "iat": now,
        "exp": now + 300,
    } | over
    return "Bearer " + gjwt.encode(signer, claims).decode()


def _ok(auth):
    return verify_proxy_caller(
        auth,
        audiences=[AUD, "https://other-allowed.example"],
        trusted_sa=SA,
        certs=lambda **_: {"k1": _PUB},
    )


def test_verify_proxy_caller_accepts_valid_proxy_token():
    assert _ok(_tok())


@pytest.mark.parametrize(
    "over",
    [
        {"email": "someone@test-project.iam.gserviceaccount.com"},
        {"aud": "https://evil.example"},
        {"email_verified": False},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 600},
    ],
    ids=["wrong-email", "aud-not-allowed", "email-unverified", "bad-iss", "expired"],
)
def test_verify_proxy_caller_rejects_bad_claims(over):
    assert not _ok(_tok(**over))


def test_verify_proxy_caller_rejects_bad_signature_and_malformed():
    assert not _ok(_tok(signer=_EVIL_SIGNER))
    assert not _ok(None) and not _ok("Basic abc") and not _ok("Bearer not-a-jwt")


def test_verify_proxy_caller_tolerates_small_clock_skew():
    now = int(time.time())
    assert _ok(_tok(iat=now + 10, exp=now + 310))


def test_verify_proxy_caller_fails_closed_when_certs_unreachable(caplog, monkeypatch):
    import runserver.authz as authz

    monkeypatch.setattr(authz, "_last_reject_log", float("-inf"))

    def _down(**_):
        raise OSError("network unreachable")

    with caplog.at_level("INFO", logger="runserver.authz"):
        tok = _tok()
        assert not verify_proxy_caller(tok, audiences=[AUD], trusted_sa=SA, certs=_down)
    assert "OSError" in caplog.text
    assert tok[7:] not in caplog.text  # never log the token


def test_verify_proxy_caller_refetches_certs_once_for_unknown_kid():
    calls = []

    def _certs(*, refresh=False):
        calls.append(refresh)
        return {"k1": _PUB} if refresh else {"old": _PUB}

    assert verify_proxy_caller(_tok(), audiences=[AUD], trusted_sa=SA, certs=_certs)
    assert calls == [False, True]


class _FakeClock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def certs_env(monkeypatch):
    """Reset the module-level certs cache; fake the clock and the HTTP fetch."""
    import io
    import json

    import runserver.authz as authz

    clock, state = _FakeClock(), {"fail": False, "fetches": 0}

    def _urlopen(url, timeout):
        state["fetches"] += 1
        if state["fail"]:
            raise OSError("down")
        return io.BytesIO(json.dumps({"k1": _PUB.decode()}).encode())

    monkeypatch.setattr(authz.time, "monotonic", clock)
    monkeypatch.setattr(authz.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(authz, "_certs", {})
    monkeypatch.setattr(authz, "_certs_at", float("-inf"))
    monkeypatch.setattr(authz, "_certs_failed_at", float("-inf"))
    return authz, clock, state


def test_google_certs_caches_for_ttl(certs_env):
    authz, clock, state = certs_env
    assert "k1" in authz.google_certs()
    clock.t += 100
    authz.google_certs()
    assert state["fetches"] == 1
    clock.t += 3600
    authz.google_certs()
    assert state["fetches"] == 2


def test_google_certs_backs_off_after_failure(certs_env):
    authz, clock, state = certs_env
    state["fail"] = True
    with pytest.raises(OSError):
        authz.google_certs()
    # During backoff: fail closed without another network attempt.
    clock.t += 10
    with pytest.raises(OSError):
        authz.google_certs()
    assert state["fetches"] == 1
    # After backoff: retry, and recover.
    state["fail"] = False
    clock.t += 60
    assert "k1" in authz.google_certs()
    assert state["fetches"] == 2


def test_google_certs_refresh_is_rate_limited(certs_env):
    authz, clock, state = certs_env
    authz.google_certs()
    authz.google_certs(refresh=True)  # just fetched: no refetch
    assert state["fetches"] == 1
    clock.t += 61
    authz.google_certs(refresh=True)
    assert state["fetches"] == 2
    # A failed refresh keeps serving the still-fresh cached certs.
    state["fail"] = True
    clock.t += 61
    assert "k1" in authz.google_certs(refresh=True)
    clock.t += 1
    authz.google_certs(refresh=True)  # in backoff: no new attempt
    assert state["fetches"] == 3


def _app(mode):
    app = FastAPI()

    @app.get("/apps/{a}/users/{u}/sessions")
    async def list_sessions(a: str, u: str):
        return {"user": u}

    @app.get("/apps/{a}/users/{u}/sessions/{s}")
    async def get(a: str, u: str, s: str):
        if s == "foreign":
            raise ValueError(f"Session {s} does not belong to user {u}.")
        if s == "boom":
            raise ValueError("some unrelated failure")
        return {"user": u}

    install_ownership_handler(app)
    app.add_middleware(
        UserAuthzMiddleware, mode=mode, caller_ok=lambda auth: auth == "Bearer proxy"
    )
    return app


def _get(app, path, **headers):
    async def go():
        t = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
            return await c.get(path, headers=headers)

    return asyncio.run(go())


_PROXY = {"authorization": "Bearer proxy", "x-tt-user": A}


def test_middleware_enforce():
    app, ok = _app(AuthzMode.ENFORCE), _PROXY
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
    assert _get(app, "/run_sse", **ok).status_code == 404


def test_middleware_normalizes_trusted_header():
    hdrs = {
        "authorization": "Bearer proxy",
        "x-tt-user": "accounts.google.com:Alice@Example.com",
    }
    assert (
        _get(
            _app(AuthzMode.ENFORCE), f"/apps/x/users/{A}/sessions/1", **hdrs
        ).status_code
        == 200
    )


def test_middleware_accepts_percent_encoded_path_user():
    # Starlette's scope["path"] is percent-decoded, so %40 compares as "@".
    resp = _get(
        _app(AuthzMode.ENFORCE),
        "/apps/a/users/alice%40x.com/sessions",
        authorization="Bearer proxy",
        **{"x-tt-user": "alice@x.com"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"user": "alice@x.com"}


def test_ownership_handler_only_maps_the_ownership_message():
    app = _app(AuthzMode.ENFORCE)
    assert _get(app, f"/apps/x/users/{A}/sessions/boom", **_PROXY).status_code == 500


def test_middleware_trust_client_passes_everything():
    assert (
        _get(_app(AuthzMode.TRUST_CLIENT), "/apps/x/users/me/sessions/1").status_code
        == 200
    )


def test_middleware_verifies_caller_off_the_event_loop():
    import threading

    seen = []

    def caller_ok(auth):
        seen.append(threading.get_ident())
        return auth == "Bearer proxy"

    app = FastAPI()

    @app.get("/apps/{a}/users/{u}/sessions")
    async def list_sessions(a: str, u: str):
        return {"loop_thread": threading.get_ident()}

    app.add_middleware(UserAuthzMiddleware, mode=AuthzMode.ENFORCE, caller_ok=caller_ok)
    resp = _get(app, f"/apps/x/users/{A}/sessions", **_PROXY)
    assert resp.status_code == 200
    assert seen and seen[0] != resp.json()["loop_thread"]


def _raw_asgi(mode, scope, messages):
    """Drive UserAuthzMiddleware directly; return (inner_app_called, sent)."""
    called, sent = [], []
    inbox = list(messages)

    async def inner(scope, receive, send):
        called.append(scope["type"])

    async def receive():
        return inbox.pop(0)

    async def send(msg):
        sent.append(msg)

    mw = UserAuthzMiddleware(inner, mode=mode, caller_ok=lambda auth: True)
    asyncio.run(mw(scope, receive, send))
    return called, sent


def _ws_scope(path, headers=()):
    return {"type": "websocket", "path": path, "headers": list(headers)}


def test_middleware_rejects_blocked_websocket_without_calling_app():
    called, sent = _raw_asgi(
        AuthzMode.ENFORCE,
        _ws_scope("/run_live", [(b"x-tt-user", A.encode())]),
        [{"type": "websocket.connect"}],
    )
    assert called == []
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_middleware_passes_allowed_websocket_and_lifespan():
    called, sent = _raw_asgi(AuthzMode.ENFORCE, _ws_scope("/some-ws"), [])
    assert called == ["websocket"] and sent == []
    called, _ = _raw_asgi(AuthzMode.ENFORCE, {"type": "lifespan"}, [])
    assert called == ["lifespan"]
