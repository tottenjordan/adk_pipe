"""Tests for deployment.register_gemini_enterprise (offline, no GCP credentials)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from deployment.register_gemini_enterprise import (
    build_agents_url,
    build_discovery_base_url,
    build_registration_payload,
    main,
    normalize_reasoning_engine_name,
    run_cli,
    validate_a2a_base_url,
    validate_location,
)


def test_global_vs_regional_discovery_engine_urls():
    assert (
        build_discovery_base_url(project="proj-1", location="global")
        == "https://discoveryengine.googleapis.com/v1alpha/projects/proj-1/locations/global/collections/default_collection"
    )
    assert (
        build_discovery_base_url(project="proj-1", location="us-central1")
        == "https://us-central1-discoveryengine.googleapis.com/v1alpha/projects/proj-1/locations/us-central1/collections/default_collection"
    )
    assert (
        build_agents_url(project="proj-1", location="eu", app_id="my-ge-app")
        == "https://eu-discoveryengine.googleapis.com/v1alpha/projects/proj-1/locations/eu/collections/default_collection/engines/my-ge-app/assistants/default_assistant/agents"
    )
    assert (
        build_agents_url(
            project="proj-1",
            location="global",
            app_id="my-ge-app",
            agent_resource_id="projects/proj-1/locations/global/collections/default_collection/engines/my-ge-app/assistants/default_assistant/agents/ag-42",
            update_mask=("displayName", "a2aAgentDefinition"),
        )
        == "https://discoveryengine.googleapis.com/v1alpha/projects/proj-1/locations/global/collections/default_collection/engines/my-ge-app/assistants/default_assistant/agents/ag-42?updateMask=displayName%2Ca2aAgentDefinition"
    )


def test_reasoning_engine_and_a2a_payloads():
    re_payload = build_registration_payload(
        agent_name="trend_scout",
        mode="reasoning-engine",
        project="proj-1",
        engine_location="us-central1",
        reasoning_engine_id="987654321",
    )
    assert re_payload["displayName"] == "Trend Trawler — Trend Scout"
    assert (
        re_payload["adkAgentDefinition"]["provisionedReasoningEngine"][
            "reasoningEngine"
        ]
        == "projects/proj-1/locations/us-central1/reasoningEngines/987654321"
    )
    assert re_payload["adkAgentDefinition"]["toolSettings"]["toolDescription"]

    a2a_payload = build_registration_payload(
        agent_name="creative_agent",
        mode="a2a",
        project="proj-1",
        a2a_base_url="https://trend-trawler-api.example.com",
    )
    card_json = a2a_payload["a2aAgentDefinition"]["jsonAgentCard"]
    parsed_card = json.loads(card_json)
    assert parsed_card["name"] == "creative_agent"
    assert (
        parsed_card["url"]
        == "https://trend-trawler-api.example.com/a2a/creative_agent/"
    )
    assert parsed_card["preferredTransport"] == "JSONRPC"


def test_dry_run_requires_no_credentials_or_network(capsys):
    def _forbidden_token():
        raise AssertionError("token_provider must not be called in --dry-run")

    def _forbidden_transport(*_args):
        raise AssertionError("transport must not be called in --dry-run")

    items = run_cli(
        [
            "--project",
            "offline-proj",
            "--location",
            "global",
            "--app-id",
            "placeholder-app",
            "--agent",
            "all",
            "--mode",
            "a2a",
            "--a2a-base-url",
            "https://trend-trawler-api.example.com",
            "--register",
            "--dry-run",
        ],
        env={},
        transport=_forbidden_transport,
        token_provider=_forbidden_token,
    )
    assert len(items) == 3
    assert [i["agent"] for i in items] == [
        "trend_scout",
        "creative_agent",
        "interactive_creative",
    ]
    out = capsys.readouterr().out
    assert "<REDACTED>" in out
    parsed = json.loads(out)
    assert len(parsed["operations"]) == 3

    # Verify main() exits 0 on --dry-run
    rc = main(
        [
            "--project",
            "offline-proj",
            "--app-id",
            "placeholder-app",
            "--agent",
            "trend_scout",
            "--mode",
            "reasoning-engine",
            "--reasoning-engine-id",
            "111222333",
            "--register",
            "--dry-run",
        ]
    )
    assert rc == 0


def test_list_register_update_delete_mocked_http():
    calls: list[dict[str, Any]] = []

    def _fake_transport(method, url, headers, payload):
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "payload": payload,
            }
        )
        return {"ok": True, "method": method}

    base_args = [
        "--project",
        "proj-live",
        "--location",
        "us-central1",
        "--app-id",
        "ge-app-1",
    ]
    token_fn = lambda: "fake-oauth-token"  # noqa: E731

    # 1. --list
    run_cli(
        [*base_args, "--list"],
        env={},
        transport=_fake_transport,
        token_provider=token_fn,
    )
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"].startswith(
        "https://us-central1-discoveryengine.googleapis.com/v1alpha/projects/proj-live/locations/us-central1/"
    )
    assert calls[-1]["headers"]["Authorization"] == "Bearer fake-oauth-token"
    assert calls[-1]["headers"]["X-Goog-User-Project"] == "proj-live"
    assert calls[-1]["payload"] is None

    # 2. --register (reasoning-engine)
    run_cli(
        [
            *base_args,
            "--agent",
            "interactive_creative",
            "--mode",
            "reasoning-engine",
            "--reasoning-engine-id",
            "555666",
            "--register",
        ],
        env={"GCP_REGION": "us-central1"},
        transport=_fake_transport,
        token_provider=token_fn,
    )
    assert calls[-1]["method"] == "POST"
    assert (
        calls[-1]["payload"]["adkAgentDefinition"]["provisionedReasoningEngine"][
            "reasoningEngine"
        ]
        == "projects/proj-live/locations/us-central1/reasoningEngines/555666"
    )

    # 3. --update (a2a)
    run_cli(
        [
            *base_args,
            "--agent",
            "creative_agent",
            "--mode",
            "a2a",
            "--a2a-base-url",
            "https://trend-trawler-api.example.com",
            "--agent-resource-id",
            "agent-res-99",
            "--update",
        ],
        env={},
        transport=_fake_transport,
        token_provider=token_fn,
    )
    assert calls[-1]["method"] == "PATCH"
    assert "/agents/agent-res-99?updateMask=" in calls[-1]["url"]
    assert "a2aAgentDefinition" in calls[-1]["payload"]

    # 4. --delete
    run_cli(
        [*base_args, "--agent-resource-id", "agent-res-99", "--delete"],
        env={},
        transport=_fake_transport,
        token_provider=token_fn,
    )
    assert calls[-1]["method"] == "DELETE"
    assert calls[-1]["url"].endswith("/agents/agent-res-99")
    assert calls[-1]["payload"] is None


def test_missing_app_id_fails_cleanly_without_hardcoded_default():
    with pytest.raises(ValueError, match="app-id"):
        run_cli(
            ["--project", "proj-1", "--list", "--dry-run"],
            env={},
        )


def test_url_authority_and_scheme_validation():
    # Reject malformed location slugs that could redirect Bearer-authenticated requests
    for bad_loc in (
        "",
        "evil.example.com#",
        "us-central1@evil.example.com",
        "../global",
        "US-CENTRAL1",
        "-leading-hyphen",
        "a" * 64,
    ):
        with pytest.raises(ValueError):
            validate_location(bad_loc)
        with pytest.raises(ValueError):
            build_discovery_base_url(project="proj-1", location=bad_loc)
        if bad_loc:
            with pytest.raises(ValueError):
                normalize_reasoning_engine_name(
                    "12345", project="proj-1", engine_location=bad_loc
                )

    assert validate_location("global") == "global"
    assert validate_location("us-central1") == "us-central1"

    # Reject non-http(s), missing host, embedded credentials, query/fragment in --a2a-base-url
    for bad_url in (
        "",
        "file:///etc/passwd",
        "ftp://internal.example.com",
        "javascript:alert(1)",
        "https://",
        "https://user:secret@trend-trawler-api.example.com",
        "https://trend-trawler-api.example.com?leak=1",
        "https://trend-trawler-api.example.com#frag",
    ):
        with pytest.raises(ValueError):
            validate_a2a_base_url(bad_url)
        with pytest.raises(ValueError):
            build_registration_payload(
                agent_name="trend_scout",
                mode="a2a",
                project="proj-1",
                a2a_base_url=bad_url,
            )

    assert (
        validate_a2a_base_url("https://trend-trawler-api.example.com/")
        == "https://trend-trawler-api.example.com"
    )
    assert validate_a2a_base_url("http://localhost:8000/") == "http://localhost:8000"
