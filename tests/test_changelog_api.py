"""Tests for runserver.changelog (/changelog and /claim-handoff)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import FastAPI
from google.adk.artifacts import InMemoryArtifactService
from google.adk.events import Event, EventActions
from google.adk.sessions import InMemorySessionService
from google.genai import types

from runserver import changelog
from runserver.a2a_mount import A2A_SERVICE_USER, mint_handoff_token
from runserver.async_runs import build_terminal_event, get_run_status
from runserver.authz import AuthzMode, UserAuthzMiddleware, install_ownership_handler

ALICE = "alice@example.com"
BOB = "bob@example.com"
SECRET = "test-handoff-secret-for-changelog-api"
_PROXY_ALICE = {"authorization": "Bearer proxy", "x-tt-user": ALICE}
_PROXY_BOB = {"authorization": "Bearer proxy", "x-tt-user": BOB}


def _make_app(
    session_service: InMemorySessionService,
    *,
    artifact_service: Any = None,
    mode: AuthzMode = AuthzMode.ENFORCE,
    handoff_secret: str | None = SECRET,
) -> FastAPI:
    changelog.configure(
        session_service=session_service,
        artifact_service=artifact_service,
        authz_mode=mode,
        handoff_secret=handoff_secret,
    )
    app = FastAPI()
    app.include_router(changelog.router)
    install_ownership_handler(app)
    app.add_middleware(
        UserAuthzMiddleware,
        mode=mode,
        caller_ok=lambda auth: auth == "Bearer proxy",
    )
    return app


def test_get_changelog_studio_ge_gates_sources_checkpoints_and_authz():
    svc = InMemorySessionService()
    app = _make_app(svc, mode=AuthzMode.ENFORCE)

    async def _run():
        sess = await svc.create_session(
            app_name="interactive_creative",
            user_id=ALICE,
            state={
                "brand": "PRS Guitars",
                "target_search_trends": "acoustic session",
                "sources": {
                    "W1": {"url": "https://a.example"},
                    "W2": {"url": "https://b.example"},
                },
                "enterprise_sources": {"E1": {"title": "Brand Playbook"}},
                "brief_revision_rounds_used": 1,
                "ad_copy_revision_rounds_used": 2,
                "visual_concept_revision_rounds_used": 0,
                "combined_final_cited_report": "# Research",
                "ad_copy_critique": {
                    "ad_copies": [{"original_id": 1, "headline": "Play on"}]
                },
                "final_visual_concepts": {
                    "visual_concepts": [{"concept_name": "Stage Glow"}]
                },
                "generated_images": {
                    "Stage Glow": {"attempts": 2, "gcs_uri": "gs://b/stage_glow.png"}
                },
                "creative_evaluation_report": {"summary": {"overall_pass_rate": 0.75}},
                "image_qa__issues": ["Minor text kerning on first draft"],
            },
        )
        # Simulate checkpoint-1 brief edit + review_research approval event
        await svc.append_event(
            sess,
            Event(
                author="__runserver__",
                invocation_id="__runserver__",
                actions=EventActions(state_delta={"creative_brief_edited": True}),
            ),
        )
        await svc.append_event(
            sess,
            Event(
                author="user",
                invocation_id="inv-2",
                content=types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                id="call-1",
                                name="review_research",
                                response={
                                    "approved": True,
                                    "feedback": "Tightened proposition",
                                },
                            )
                        )
                    ],
                ),
            ),
        )

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            resp = await client.get(
                f"/runs/interactive_creative/{ALICE}/{sess.id}/changelog",
                headers=_PROXY_ALICE,
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["session_id"] == sess.id
            assert body["app_name"] == "interactive_creative"
            assert body["origin"] == "studio"
            assert body["handoff_source_session_id"] is None
            assert body["gate_rounds"] == {
                "brief": 1,
                "ad_copy": 2,
                "visual_concept": 0,
            }
            assert body["source_counts"] == {"web": 2, "enterprise": 1}
            assert any("Image check" in w for w in body["warnings"])

            kinds = [e["kind"] for e in body["entries"]]
            assert "origin" in kinds
            assert "research" in kinds
            assert "checkpoint" in kinds
            assert "gate" in kinds
            assert "image_qa" in kinds
            assert "evaluation" in kinds
            assert any("Edited creative brief" in e["label"] for e in body["entries"])
            assert any(
                "1 QA re-render(s) across Stage Glow" in (e.get("detail") or "")
                for e in body["entries"]
            )

            # Cross-user -> 403
            cross = await client.get(
                f"/runs/interactive_creative/{BOB}/{sess.id}/changelog",
                headers=_PROXY_ALICE,
            )
            assert cross.status_code == 403

            # Unauthenticated -> 401
            unauth = await client.get(
                f"/runs/interactive_creative/{ALICE}/{sess.id}/changelog"
            )
            assert unauth.status_code == 401

            # Missing / unowned session -> 404
            missing = await client.get(
                f"/runs/interactive_creative/{ALICE}/nonexistent-sid/changelog",
                headers=_PROXY_ALICE,
            )
            assert missing.status_code == 404

    asyncio.run(_run())


def test_claim_handoff_clones_state_idempotently_and_enforces_token_and_secret():
    svc = InMemorySessionService()
    app = _make_app(svc, mode=AuthzMode.ENFORCE, handoff_secret=SECRET)

    async def _run():
        source = await svc.create_session(
            app_name="creative_agent",
            user_id=A2A_SERVICE_USER,
            state={
                "run_origin": "a2a",
                "brand": "PRS Guitars",
                "creative_brief_edited": True,
                "sources": {"W1": {"url": "https://prs.example"}},
            },
        )
        valid_token = mint_handoff_token(
            "creative_agent", source.id, secret=SECRET, ttl_seconds=600
        )
        expired_token = mint_handoff_token(
            "creative_agent", source.id, secret=SECRET, ttl_seconds=10, now=100.0
        )

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            # Invalid / expired / non-ASCII token -> 403 (never 500)
            bad = await client.post(
                f"/runs/creative_agent/{ALICE}/{source.id}/claim-handoff",
                json={"handoff": "tampered.token"},
                headers=_PROXY_ALICE,
            )
            assert bad.status_code == 403

            non_ascii = await client.post(
                f"/runs/creative_agent/{ALICE}/{source.id}/claim-handoff",
                json={"handoff": "abc.\u00e9"},
                headers=_PROXY_ALICE,
            )
            assert non_ascii.status_code == 403

            exp = await client.post(
                f"/runs/creative_agent/{ALICE}/{source.id}/claim-handoff",
                json={"handoff": expired_token},
                headers=_PROXY_ALICE,
            )
            assert exp.status_code == 403

            # Cross-user claim attempt -> 403
            cross = await client.post(
                f"/runs/creative_agent/{BOB}/{source.id}/claim-handoff",
                json={"handoff": valid_token},
                headers=_PROXY_ALICE,
            )
            assert cross.status_code == 403

            # Valid claim -> clones state to ALICE with run_origin="gemini_enterprise"
            c1 = await client.post(
                f"/runs/creative_agent/{ALICE}/{source.id}/claim-handoff",
                json={"handoff": valid_token},
                headers=_PROXY_ALICE,
            )
            assert c1.status_code == 200
            body1 = c1.json()
            new_sid = body1["session_id"]
            assert new_sid != source.id
            assert body1 == {
                "session_id": new_sid,
                "app_name": "creative_agent",
                "already_owned": False,
            }

            # Idempotent re-claim (e.g. StrictMode double-mount) returns the exact same new_sid!
            c2 = await client.post(
                f"/runs/creative_agent/{ALICE}/{source.id}/claim-handoff",
                json={"handoff": valid_token},
                headers=_PROXY_ALICE,
            )
            assert c2.status_code == 200
            assert c2.json()["session_id"] == new_sid

            # Single-owner binding: BOB replaying ALICE's already-claimed token gets 403!
            bob_replay = await client.post(
                f"/runs/creative_agent/{BOB}/{source.id}/claim-handoff",
                json={"handoff": valid_token},
                headers=_PROXY_BOB,
            )
            assert bob_replay.status_code == 403
            assert "already been claimed" in bob_replay.json()["detail"]

            # Even after in-memory cache eviction/restart, durable source_session markers
            # keep ALICE idempotent (200, same new_sid) and reject BOB with 403!
            with changelog._CLAIMED_CACHE_LOCK:
                changelog._CLAIMED_SESSIONS.clear()
            bob_after_evict = await client.post(
                f"/runs/creative_agent/{BOB}/{source.id}/claim-handoff",
                json={"handoff": valid_token},
                headers=_PROXY_BOB,
            )
            assert bob_after_evict.status_code == 403
            alice_after_evict = await client.post(
                f"/runs/creative_agent/{ALICE}/{source.id}/claim-handoff",
                json={"handoff": valid_token},
                headers=_PROXY_ALICE,
            )
            assert alice_after_evict.status_code == 200
            assert alice_after_evict.json()["session_id"] == new_sid

            # Inspect changelog of the newly claimed session under ALICE
            cl = await client.get(
                f"/runs/creative_agent/{ALICE}/{new_sid}/changelog",
                headers=_PROXY_ALICE,
            )
            assert cl.status_code == 200
            cl_body = cl.json()
            assert cl_body["origin"] == "gemini_enterprise"
            assert cl_body["handoff_source_session_id"] == source.id
            assert cl_body["source_counts"] == {"web": 1, "enterprise": 0}

            # Already-owned session claim returns already_owned=True
            owned_tok = mint_handoff_token(
                "creative_agent", new_sid, secret=SECRET, ttl_seconds=600
            )
            c_owned = await client.post(
                f"/runs/creative_agent/{ALICE}/{new_sid}/claim-handoff",
                json={"handoff": owned_tok},
                headers=_PROXY_ALICE,
            )
            assert c_owned.status_code == 200
            assert c_owned.json() == {
                "session_id": new_sid,
                "app_name": "creative_agent",
                "already_owned": True,
            }

        # Missing A2A_HANDOFF_SECRET -> 503
        unconfigured_app = _make_app(svc, mode=AuthzMode.ENFORCE, handoff_secret="")
        transport2 = httpx.ASGITransport(
            app=unconfigured_app, raise_app_exceptions=False
        )
        async with httpx.AsyncClient(
            transport=transport2, base_url="http://t"
        ) as client2:
            no_sec = await client2.post(
                f"/runs/creative_agent/{ALICE}/{source.id}/claim-handoff",
                json={"handoff": valid_token},
                headers=_PROXY_ALICE,
            )
            assert no_sec.status_code == 503

    asyncio.run(_run())


def test_claim_handoff_replays_paused_checkpoint_events_for_run_status():
    svc = InMemorySessionService()
    app = _make_app(svc, mode=AuthzMode.ENFORCE, handoff_secret=SECRET)

    async def _run():
        source = await svc.create_session(
            app_name="interactive_creative",
            user_id=A2A_SERVICE_USER,
            state={
                "run_origin": "a2a",
                "brand": "PRS Guitars",
                "creative_brief": {"single_minded_proposition": "Play with soul"},
            },
        )
        user_ev = Event(
            author="user",
            invocation_id="inv-pause-1",
            content=types.Content(
                role="user", parts=[types.Part(text="Start interactive run")]
            ),
            actions=EventActions(state_delta={"run_origin": "a2a"}),
        )
        await svc.append_event(source, user_ev)

        # Partial streaming chunk (must not be persisted into claimed session)
        partial_ev = Event(
            author="root_agent",
            invocation_id="inv-pause-1",
            partial=True,
            content=types.Content(role="model", parts=[types.Part(text="Drafting...")]),
        )
        source.events.append(partial_ev)

        pause_ev = Event(
            author="root_agent",
            invocation_id="inv-pause-1",
            long_running_tool_ids={"call-review-1"},
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="call-review-1",
                            name="review_research",
                            args={"summary": "Review creative brief"},
                        )
                    )
                ],
            ),
            actions=EventActions(agent_state={"root_agent": {"checkpoint": 1}}),
        )
        await svc.append_event(source, pause_ev)
        await svc.append_event(source, build_terminal_event("done"))

        source_event_ids = {ev.id for ev in source.events}
        token = mint_handoff_token(
            "interactive_creative", source.id, secret=SECRET, ttl_seconds=600
        )

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            resp = await client.post(
                f"/runs/interactive_creative/{ALICE}/{source.id}/claim-handoff",
                json={"handoff": token},
                headers=_PROXY_ALICE,
            )
            assert resp.status_code == 200
            new_sid = resp.json()["session_id"]

        poll = await get_run_status(
            app_name="interactive_creative",
            user_id=ALICE,
            session_id=new_sid,
            since=0,
            session_service=svc,
        )
        assert poll["status"] == "done"
        assert poll["nextCursor"] >= 3
        assert len(poll["events"]) == poll["nextCursor"]
        assert poll["state"]["run_origin"] == "gemini_enterprise"
        assert poll["state"]["handoff_source_session_id"] == source.id
        assert poll["state"]["ui_app"] == "interactive_creative"

        # Every replayed event got a fresh unique event ID and no partial event leaked
        claimed_ids = {ev["id"] for ev in poll["events"]}
        assert claimed_ids.isdisjoint(source_event_ids)
        assert not any(ev.get("partial") for ev in poll["events"])

        # Replayed user event stateDelta does not revert run_origin back to 'a2a'
        for ev in poll["events"]:
            sd = (ev.get("actions") or {}).get("stateDelta") or {}
            if "run_origin" in sd:
                assert sd["run_origin"] == "gemini_enterprise"

        # Pending LongRunningFunctionTool call + agentState are intact for Studio resume
        checkpoint_events = [
            ev
            for ev in poll["events"]
            if "call-review-1" in (ev.get("longRunningToolIds") or [])
        ]
        assert len(checkpoint_events) == 1
        cp = checkpoint_events[0]
        assert cp["invocationId"] == "inv-pause-1"
        assert cp["actions"]["agentState"] == {"root_agent": {"checkpoint": 1}}
        fn_calls = [
            p["functionCall"] for p in cp["content"]["parts"] if "functionCall" in p
        ]
        assert len(fn_calls) == 1
        assert fn_calls[0]["id"] == "call-review-1"
        assert fn_calls[0]["name"] == "review_research"

    asyncio.run(_run())


def test_claim_handoff_copies_session_artifacts_and_skips_user_scoped_keys():
    svc = InMemorySessionService()
    art_svc = InMemoryArtifactService()
    app = _make_app(
        svc, artifact_service=art_svc, mode=AuthzMode.ENFORCE, handoff_secret=SECRET
    )

    async def _run():
        source = await svc.create_session(
            app_name="creative_agent",
            user_id=A2A_SERVICE_USER,
            state={"run_origin": "a2a", "brand": "PRS Guitars"},
        )
        pdf_part = types.Part(
            inline_data=types.Blob(mime_type="application/pdf", data=b"%PDF-1.4-test")
        )
        html_part = types.Part(text="<html><body>Gallery</body></html>")
        user_scoped_part = types.Part(text='{"shared_service_pref": true}')

        await art_svc.save_artifact(
            app_name="creative_agent",
            user_id=A2A_SERVICE_USER,
            session_id=source.id,
            filename="research_report.pdf",
            artifact=pdf_part,
        )
        await art_svc.save_artifact(
            app_name="creative_agent",
            user_id=A2A_SERVICE_USER,
            session_id=source.id,
            filename="gallery.html",
            artifact=html_part,
        )
        await art_svc.save_artifact(
            app_name="creative_agent",
            user_id=A2A_SERVICE_USER,
            session_id=source.id,
            filename="user:service_prefs.json",
            artifact=user_scoped_part,
        )

        token = mint_handoff_token(
            "creative_agent", source.id, secret=SECRET, ttl_seconds=600
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            resp = await client.post(
                f"/runs/creative_agent/{ALICE}/{source.id}/claim-handoff",
                json={"handoff": token},
                headers=_PROXY_ALICE,
            )
            assert resp.status_code == 200
            new_sid = resp.json()["session_id"]

        copied_keys = await art_svc.list_artifact_keys(
            app_name="creative_agent", user_id=ALICE, session_id=new_sid
        )
        assert copied_keys == ["gallery.html", "research_report.pdf"]

        loaded_pdf = await art_svc.load_artifact(
            app_name="creative_agent",
            user_id=ALICE,
            session_id=new_sid,
            filename="research_report.pdf",
        )
        assert loaded_pdf is not None
        assert loaded_pdf.inline_data is not None
        assert loaded_pdf.inline_data.data == b"%PDF-1.4-test"

        loaded_html = await art_svc.load_artifact(
            app_name="creative_agent",
            user_id=ALICE,
            session_id=new_sid,
            filename="gallery.html",
        )
        assert loaded_html is not None
        assert loaded_html.text == "<html><body>Gallery</body></html>"

        # Fail-soft: broken artifact service never breaks claim-handoff
        class _BrokenArtifacts:
            async def list_artifact_keys(self, **_kw):
                raise RuntimeError("artifact backend down")

        broken_app = _make_app(
            svc,
            artifact_service=_BrokenArtifacts(),
            mode=AuthzMode.ENFORCE,
            handoff_secret=SECRET,
        )
        source2 = await svc.create_session(
            app_name="creative_agent",
            user_id=A2A_SERVICE_USER,
            state={"run_origin": "a2a"},
        )
        tok2 = mint_handoff_token(
            "creative_agent", source2.id, secret=SECRET, ttl_seconds=600
        )
        transport2 = httpx.ASGITransport(app=broken_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport2, base_url="http://t"
        ) as client2:
            r2 = await client2.post(
                f"/runs/creative_agent/{ALICE}/{source2.id}/claim-handoff",
                json={"handoff": tok2},
                headers=_PROXY_ALICE,
            )
            assert r2.status_code == 200

    asyncio.run(_run())


def test_claim_handoff_concurrent_race_across_multiple_event_loops():
    svc = InMemorySessionService()
    app = _make_app(svc, mode=AuthzMode.ENFORCE, handoff_secret=SECRET)

    # Event loop #1: concurrent same-user StrictMode race + Alice-vs-Bob race
    async def _loop_one():
        src_same = await svc.create_session(
            app_name="interactive_creative",
            user_id=A2A_SERVICE_USER,
            state={"run_origin": "a2a"},
        )
        tok_same = mint_handoff_token(
            "interactive_creative", src_same.id, secret=SECRET, ttl_seconds=600
        )

        src_race = await svc.create_session(
            app_name="interactive_creative",
            user_id=A2A_SERVICE_USER,
            state={"run_origin": "a2a"},
        )
        tok_race = mint_handoff_token(
            "interactive_creative", src_race.id, secret=SECRET, ttl_seconds=600
        )

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            same_resps = await asyncio.gather(
                *[
                    client.post(
                        f"/runs/interactive_creative/{ALICE}/{src_same.id}/claim-handoff",
                        json={"handoff": tok_same},
                        headers=_PROXY_ALICE,
                    )
                    for _ in range(4)
                ]
            )
            assert all(r.status_code == 200 for r in same_resps)
            assert len({r.json()["session_id"] for r in same_resps}) == 1

            race_resps = await asyncio.gather(
                client.post(
                    f"/runs/interactive_creative/{ALICE}/{src_race.id}/claim-handoff",
                    json={"handoff": tok_race},
                    headers=_PROXY_ALICE,
                ),
                client.post(
                    f"/runs/interactive_creative/{BOB}/{src_race.id}/claim-handoff",
                    json={"handoff": tok_race},
                    headers=_PROXY_BOB,
                ),
            )
            assert sorted(r.status_code for r in race_resps) == [200, 403]

    # Event loop #2: separate asyncio.run() without re-importing module -> verifies no stale loop-bound Lock
    async def _loop_two():
        src2 = await svc.create_session(
            app_name="trend_scout",
            user_id=A2A_SERVICE_USER,
            state={"run_origin": "a2a"},
        )
        tok2 = mint_handoff_token(
            "trend_scout", src2.id, secret=SECRET, ttl_seconds=600
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            resps = await asyncio.gather(
                client.post(
                    f"/runs/trend_scout/{ALICE}/{src2.id}/claim-handoff",
                    json={"handoff": tok2},
                    headers=_PROXY_ALICE,
                ),
                client.post(
                    f"/runs/trend_scout/{ALICE}/{src2.id}/claim-handoff",
                    json={"handoff": tok2},
                    headers=_PROXY_ALICE,
                ),
            )
            assert [r.status_code for r in resps] == [200, 200]
            assert resps[0].json()["session_id"] == resps[1].json()["session_id"]

    asyncio.run(_loop_one())
    asyncio.run(_loop_two())
