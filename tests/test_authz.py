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


@pytest.mark.parametrize(
    "path", ["/agent-identity/finalize", "/agent-identity/finalize/"]
)
def test_decide_blocks_agent_identity_finalize(path):
    assert decide(AuthzMode.ENFORCE, path, A) == (404, "Not found")


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
