"""runserver/variants.py: owner-scoped personalised variant previews (offline).

In-memory session service + ``InMemoryPersonRefsStore`` + a fake GCS client and
a fake renderer, driven over ``httpx.ASGITransport`` with the real
``UserAuthzMiddleware`` in front. The default renderer (``render_variant``) is
tested against a fake image client at the bottom.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from google.adk.sessions import InMemorySessionService
from google.genai import types

from runserver import person_refs as pr
from runserver import variants as vr
from runserver.authz import AuthzMode, UserAuthzMiddleware, install_ownership_handler
from runserver.person_refs_store import InMemoryPersonRefsStore, utcnow
from tests._creative_fixtures import BUCKET, CONCEPTS, creative_state

A = "alice@example.com"
B = "bob@example.com"
APP = "creative_agent"
CASTABLE = "The Authentic Encore"  # Candid 35mm film photo of a guitarist
MEME = "The Golden Golf Cart Gig"  # Surreal Meme-Collage (not person-safe)
PHOTO_A = f"gs://{BUCKET}/person-refs/{pr.slug_for(A)}/me.jpg"
PHOTO_A2 = f"gs://{BUCKET}/person-refs/{pr.slug_for(A)}/friend.png"
KEY_RE = re.compile(r"^[0-9a-f]{12}$")


def ready_state() -> dict:
    """A finished creative run (``finalize_done``) with every image rendered."""
    state = creative_state()
    state["finalize_done"] = True
    return state


class FakeBlob:
    def __init__(self, gcs: FakeGCS, bucket: str, name: str):
        self.gcs, self.bucket, self.name = gcs, bucket, name
        self.metadata: dict | None = None
        self.cache_control: str | None = None

    def upload_from_string(self, data, content_type=None):
        self.gcs.objects[(self.bucket, self.name)] = {
            "data": data,
            "content_type": content_type,
            "metadata": self.metadata,
            "cache_control": self.cache_control,
        }

    def delete(self):
        self.gcs.objects.pop((self.bucket, self.name), None)


class FakeGCS:
    def __init__(self):
        self.objects: dict[tuple[str, str], dict] = {}

    def _get_blob(self, name: str, path: str):
        obj = self.objects.get((name, path))
        if obj is None:
            return None
        blob = FakeBlob(self, name, path)
        blob.metadata = obj["metadata"]
        return blob

    def bucket(self, name: str):
        return SimpleNamespace(
            blob=lambda path: FakeBlob(self, name, path),
            get_blob=lambda path: self._get_blob(name, path),
        )


class FakeRenderer:
    def __init__(self, outcome=None, gate: asyncio.Event | None = None):
        self.calls: list[tuple[dict, dict, str]] = []
        self.outcome = outcome or vr.VariantRender(
            "done",
            image_bytes=b"variant",
            qa={"passed": True, "failures": []},
            attempts=1,
        )
        self.gate = gate
        self.started = asyncio.Event()

    async def __call__(self, state, concept, photo_uri):
        self.calls.append((dict(state), dict(concept), photo_uri))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class Harness:
    def __init__(self, mode=AuthzMode.TRUST_CLIENT, renderer=None, **kw):
        self.svc = InMemorySessionService()
        self.store = InMemoryPersonRefsStore()
        self.gcs = FakeGCS()
        self.renderer = renderer or FakeRenderer()
        pr.configure(store=self.store, bucket=BUCKET, gcs_client=self.gcs)
        vr.configure(
            session_service=self.svc,
            gcs_client=self.gcs,
            bucket=BUCKET,
            renderer=self.renderer,
            **kw,
        )
        app = FastAPI()
        app.include_router(vr.router)
        install_ownership_handler(app)
        app.add_middleware(
            UserAuthzMiddleware, mode=mode, caller_ok=lambda a: a == "Bearer proxy"
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    async def consent(self, cid="consent-aaaa", owner=A, photo=PHOTO_A):
        await self.store.put(
            {
                "consent_id": cid,
                "owner_user": owner,
                "photo_uri": photo,
                "label": "Me",
                "subject": "self",
                "adult_attested": True,
                "allow_public_share": False,
                "consent_text_version": pr.CONSENT_TEXT_VERSION,
                "created_at": utcnow(),
                "revoked_at": None,
                "person_renders": [],
            }
        )
        return cid

    async def session(self, user=A, sid="s1", state=None):
        await self.svc.create_session(
            app_name=APP,
            user_id=user,
            session_id=sid,
            state=ready_state() if state is None else state,
        )

    async def post(self, concept=CASTABLE, cid="consent-aaaa", user=A, sid="s1", **kw):
        return await self.client.post(
            f"/variants/{user}/{APP}/{sid}",
            json={"concept_name": concept, "consent_id": cid},
            **kw,
        )

    async def get(self, user=A, sid="s1", **kw):
        return await self.client.get(f"/variants/{user}/{APP}/{sid}", **kw)

    async def settle(self):
        while vr._TASKS:
            await asyncio.gather(*list(vr._TASKS.values()))

    async def state(self, user=A, sid="s1"):
        s = await self.svc.get_session(app_name=APP, user_id=user, session_id=sid)
        assert s is not None
        return s


def run(coro_fn):
    return asyncio.run(coro_fn())


# --- pure helpers -------------------------------------------------------------------


def test_variant_key_and_paths():
    key = vr.variant_key(PHOTO_A, "prompt", "model")
    assert KEY_RE.match(key) and key == vr.variant_key(PHOTO_A, "prompt", "model")
    assert key != vr.variant_key(PHOTO_A2, "prompt", "model")
    assert key != vr.variant_key(PHOTO_A, "prompt", "other-model")
    state = {"gcs_folder": "2026_run", "agent_output_dir": "creative_output"}
    path = vr.variant_object_path(state, A, "The Authentic Encore!", key)
    assert path == (
        f"2026_run/creative_output/variants/{pr.slug_for(A)}/"
        f"The_Authentic_Encore/{key}.png"
    )
    for bad in (
        {"gcs_folder": "person-refs", "agent_output_dir": "x"},
        {"gcs_folder": "shares", "agent_output_dir": "x"},
        {"gcs_folder": "a/b", "agent_output_dir": "x"},
        {"gcs_folder": "..", "agent_output_dir": "x"},
        {"gcs_folder": "run", "agent_output_dir": "variants"},
        {"gcs_folder": "run"},
    ):
        try:
            vr.variant_object_path(bad, A, "c", key)
        except vr.VariantError as exc:
            assert exc.reason == "invalid_output_folder"
        else:
            raise AssertionError(bad)


def test_variant_concept_casts_and_points_at_the_photo():
    out = vr.variant_concept({"image_generation_prompt": "a photo of a guitarist"})
    assert out["casts_person_reference"] is True
    assert out["image_generation_prompt"].endswith(
        "The hero is the person in the person reference image."
    )
    again = vr.variant_concept(out)
    assert again["image_generation_prompt"] == out["image_generation_prompt"]


# --- POST / GET ---------------------------------------------------------------------


def test_render_preview_happy_path_and_state_isolation(caplog):
    async def go():
        h = Harness()
        await h.consent()
        await h.session()
        before = copy.deepcopy(dict((await h.state()).state))
        events_before = len((await h.state()).events)
        with caplog.at_level(logging.DEBUG):
            r = await h.post()
            assert r.status_code == 200, r.text
            out = r.json()
            assert out["status"] == "queued" and out["cached"] is False
            assert KEY_RE.match(out["key"]) and out["consent_id"] == "consent-aaaa"
            await h.settle()

        listed = (await h.get()).json()["variants"]
        record = listed[CASTABLE][out["key"]]
        assert record["status"] == "done" and record["attempts"] == 1
        assert record["qa"] == {"passed": True, "failures": []}
        path = (
            f"{before['gcs_folder']}/{before['agent_output_dir']}/variants/"
            f"{pr.slug_for(A)}/The_Authentic_Encore/{out['key']}.png"
        )
        assert record["gcs_uri"] == f"gs://{BUCKET}/{path}"
        blob = h.gcs.objects[(BUCKET, path)]
        assert blob["data"] == b"variant" and blob["content_type"] == "image/png"
        assert blob["metadata"] == {"consent_id": "consent-aaaa"}
        assert blob["cache_control"] == "private, no-store"
        # recorded on the consent so revoking it deletes the variant
        assert (await h.store.get("consent-aaaa"))["person_renders"] == [
            record["gcs_uri"]
        ]

        # The renderer got the stored concept and the consent's photo.
        ((state_arg, concept_arg, photo),) = h.renderer.calls
        assert photo == PHOTO_A and concept_arg["concept_name"] == CASTABLE
        assert state_arg["brand"] == before["brand"]

        # Every appended event carries ONLY person_variants; nothing else moved.
        session = await h.state()
        new_events = session.events[events_before:]
        assert [
            e.actions.state_delta["person_variants"][CASTABLE][out["key"]]["status"]
            for e in new_events
        ] == ["queued", "rendering", "done"]
        for event in new_events:
            assert set(event.actions.state_delta) == {"person_variants"}
        after = dict(session.state)
        for key in ("final_visual_concepts", "generated_images"):
            assert after[key] == before[key]
        assert after.get("_generated_artifact_keys") == before.get(
            "_generated_artifact_keys"
        )
        assert set(after) - set(before) == {"person_variants"}
        assert PHOTO_A not in caplog.text and "me.jpg" not in caplog.text
        assert PHOTO_A not in r.text

    run(go)


def test_repeat_request_returns_the_cached_render():
    async def go():
        h = Harness()
        await h.consent()
        await h.session()
        first = (await h.post()).json()
        await h.settle()
        again = await h.post()
        assert again.status_code == 200
        body = again.json()
        assert body["cached"] is True and body["status"] == "done"
        assert body["key"] == first["key"]
        assert len(h.renderer.calls) == 1

    run(go)


def test_repeat_while_rendering_joins_the_live_render():
    async def go():
        gate = asyncio.Event()
        h = Harness(renderer=FakeRenderer(gate=gate))
        await h.consent()
        await h.session()
        first = (await h.post()).json()
        await h.renderer.started.wait()
        second = (await h.post()).json()
        assert second["key"] == first["key"] and second["cached"] is False
        assert second["status"] in ("queued", "rendering")
        gate.set()
        await h.settle()
        assert len(h.renderer.calls) == 1

    run(go)


def test_renders_wait_for_the_semaphore():
    async def go():
        gate = asyncio.Event()
        h = Harness(renderer=FakeRenderer(gate=gate), concurrency=1)
        await h.consent()
        await h.consent("consent-bbbb", photo=PHOTO_A2)
        await h.session()
        one = (await h.post()).json()
        await h.renderer.started.wait()
        two = (await h.post(cid="consent-bbbb")).json()
        await asyncio.sleep(0.05)
        listed = (await h.get()).json()["variants"][CASTABLE]
        assert listed[one["key"]]["status"] == "rendering"
        assert listed[two["key"]]["status"] == "queued"
        assert len(h.renderer.calls) == 1
        gate.set()
        await h.settle()
        listed = (await h.get()).json()["variants"][CASTABLE]
        assert {r["status"] for r in listed.values()} == {"done"}

    run(go)


def test_daily_cap():
    async def go():
        h = Harness(daily_cap=1)
        await h.consent()
        await h.consent("consent-bbbb", photo=PHOTO_A2)
        await h.session()
        assert (await h.post()).status_code == 200
        await h.settle()
        r = await h.post(cid="consent-bbbb")
        assert r.status_code == 429
        assert r.json()["detail"]["reason"] == "variant_cap_reached"
        # A cache hit is still served at the cap.
        assert (await h.post()).json()["cached"] is True

    run(go)


def test_daily_cap_counts_todays_variants_in_state():
    async def go():
        h = Harness(daily_cap=1)
        await h.consent()
        state = ready_state()
        today = vr._iso(vr._now())
        state["person_variants"] = {
            MEME: {"abc": {"status": "failed", "created_at": today}}
        }
        await h.session(state=state)
        r = await h.post()
        assert r.status_code == 429

    run(go)


def test_ownership_and_authz():
    async def go():
        h = Harness()
        await h.consent()
        await h.session()
        # Bob can't reach Alice's session (unknown for his user id).
        r = await h.post(user=B)
        assert r.status_code == 404
        assert (await h.get(user=B)).status_code == 404

        enforce = Harness(mode=AuthzMode.ENFORCE)
        await enforce.consent()
        await enforce.session()
        headers = {"Authorization": "Bearer proxy", "X-TT-User": B}
        r = await enforce.post(headers=headers)
        assert r.status_code == 403
        assert (await enforce.post()).status_code == 401  # no trusted identity

    run(go)


def test_consent_must_be_active_and_the_callers():
    async def go():
        h = Harness()
        await h.consent()
        await h.consent("consent-bob1", owner=B, photo=PHOTO_A2)
        await h.session()
        r = await h.post(cid="consent-bob1")
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "consent_not_active"
        await h.store.revoke("consent-aaaa", A)
        r = await h.post()
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "consent_not_active"
        assert (await h.post(cid="nope")).status_code == 400
        assert h.renderer.calls == []

    run(go)


def test_concept_checks():
    async def go():
        h = Harness()
        await h.consent()
        state = ready_state()
        state["generated_images"][CONCEPTS[2]]["cast"] = True
        await h.session(state=state)
        r = await h.post(concept="Nope")
        assert r.json()["detail"]["reason"] == "concept_not_found"
        r = await h.post(concept=CONCEPTS[2])
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "concept_already_cast"
        r = await h.post(concept=MEME)
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "concept_not_castable"
        assert "person-safe" in r.json()["detail"]["message"]
        bad_app = await h.client.post(
            f"/variants/{A}/trend_scout/s1",
            json={"concept_name": CASTABLE, "consent_id": "consent-aaaa"},
        )
        assert bad_app.json()["detail"]["reason"] == "invalid_app_name"
        assert h.renderer.calls == []

    run(go)


def test_unsafe_output_folder_is_refused():
    async def go():
        h = Harness()
        await h.consent()
        state = ready_state()
        state["gcs_folder"] = "person-refs"
        await h.session(state=state)
        r = await h.post()
        assert r.json()["detail"]["reason"] == "invalid_output_folder"

    run(go)


def test_refused_while_the_run_is_live(monkeypatch):
    async def go():
        h = Harness()
        await h.consent()
        await h.session()
        monkeypatch.setattr(vr, "_run_is_live", lambda *a: True)
        r = await h.post()
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "run_in_progress"

    run(go)


def test_rejected_and_failed_renders_upload_nothing():
    async def go():
        rejected = vr.VariantRender("rejected", reason="image_safety")
        h = Harness(renderer=FakeRenderer(outcome=rejected))
        await h.consent()
        await h.session()
        key = (await h.post()).json()["key"]
        await h.settle()
        record = (await h.get()).json()["variants"][CASTABLE][key]
        assert record["status"] == "rejected" and record["reason"] == "image_safety"
        assert record["gcs_uri"] is None and h.gcs.objects == {}

        boom = Harness(renderer=FakeRenderer(outcome=RuntimeError("quota")))
        await boom.consent()
        await boom.session()
        key = (await boom.post()).json()["key"]
        await boom.settle()
        record = (await boom.get()).json()["variants"][CASTABLE][key]
        assert record["status"] == "failed" and record["reason"] == "render_error"

    run(go)


def test_consent_revoked_during_the_render_uploads_nothing():
    async def go():
        gate = asyncio.Event()
        h = Harness(renderer=FakeRenderer(gate=gate))
        await h.consent()
        await h.session()
        key = (await h.post()).json()["key"]
        await h.renderer.started.wait()
        await h.store.revoke("consent-aaaa", A)
        gate.set()
        await h.settle()
        record = (await h.get()).json()["variants"][CASTABLE][key]
        assert record["status"] == "failed" and record["reason"] == "consent_revoked"
        assert record["gcs_uri"] is None and h.gcs.objects == {}

    run(go)


def test_consent_revoked_during_the_upload_deletes_the_variant(monkeypatch):
    async def go():
        h = Harness()
        await h.consent()
        await h.session()
        real_upload = vr._upload

        def upload_then_revoke(*args):
            real_upload(*args)
            h.store.rows["consent-aaaa"]["revoked_at"] = utcnow()

        monkeypatch.setattr(vr, "_upload", upload_then_revoke)
        key = (await h.post()).json()["key"]
        await h.settle()
        record = (await h.get()).json()["variants"][CASTABLE][key]
        assert record["status"] == "failed" and record["reason"] == "consent_revoked"
        assert record["gcs_uri"] is None and h.gcs.objects == {}
        # still recorded, so a retried consent revoke covers it too
        assert len((await h.store.get("consent-aaaa"))["person_renders"]) == 1

    run(go)


def test_render_record_failure_uploads_nothing(monkeypatch):
    async def go():
        h = Harness()
        await h.consent()
        await h.session()

        async def boom(*_a):
            raise RuntimeError("bq down")

        monkeypatch.setattr(h.store, "add_renders", boom)
        key = (await h.post()).json()["key"]
        await h.settle()
        record = (await h.get()).json()["variants"][CASTABLE][key]
        assert record["status"] == "failed" and record["reason"] == "render_error"
        assert h.gcs.objects == {}

    run(go)


def test_concurrent_requests_for_one_key_render_and_count_once():
    async def go():
        gate = asyncio.Event()
        h = Harness(renderer=FakeRenderer(gate=gate), daily_cap=1)
        await h.consent()
        await h.session()
        real_get_session = h.svc.get_session

        async def slow_get_session(**kw):
            await asyncio.sleep(0.01)  # let the two requests interleave
            return await real_get_session(**kw)

        h.svc.get_session = slow_get_session  # type: ignore[method-assign]
        one, two = await asyncio.gather(h.post(), h.post())
        assert one.status_code == 200 and two.status_code == 200, (one.text, two.text)
        assert one.json()["key"] == two.json()["key"]
        gate.set()
        await h.settle()
        assert len(h.renderer.calls) == 1
        assert vr._STARTED[(A, vr._now().date().isoformat())] == 1

    run(go)


def test_concept_must_be_finished_and_rendered():
    async def go():
        h = Harness()
        await h.consent()
        unfinished = creative_state()  # no finalize_done (run or checkpoint pending)
        await h.session(sid="s-unfinished", state=unfinished)
        r = await h.post(sid="s-unfinished")
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "concept_not_ready"

        no_image = ready_state()
        del no_image["generated_images"][CASTABLE]
        await h.session(sid="s-no-image", state=no_image)
        r = await h.post(sid="s-no-image")
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "concept_not_ready"
        assert h.renderer.calls == []

    run(go)


def test_orphaned_pending_record_reads_as_interrupted_and_can_retry():
    async def go():
        h = Harness()
        await h.consent()
        state = ready_state()
        key = vr.variant_key(
            PHOTO_A,
            vr.find_concept(state, CASTABLE)["image_generation_prompt"],
            vr._image_model(),
        )
        state["person_variants"] = {
            CASTABLE: {key: {"status": "rendering", "created_at": "2026-01-01"}}
        }
        await h.session(state=state)
        record = (await h.get()).json()["variants"][CASTABLE][key]
        assert record["status"] == "failed" and record["reason"] == "interrupted"
        r = await h.post()
        assert r.json()["status"] == "queued"
        await h.settle()
        assert len(h.renderer.calls) == 1

    run(go)


# --- default renderer ---------------------------------------------------------------


def _image_response(data=b"img", blocked=False):
    if blocked:
        candidate = SimpleNamespace(
            content=SimpleNamespace(parts=[]),
            finish_reason=types.FinishReason.IMAGE_SAFETY,
        )
    else:
        inline = SimpleNamespace(data=data, mime_type="image/png")
        candidate = SimpleNamespace(
            content=SimpleNamespace(
                parts=[SimpleNamespace(inline_data=inline, thought=False)]
            ),
            finish_reason=types.FinishReason.STOP,
        )
    return SimpleNamespace(candidates=[candidate], prompt_feedback=None)


def _patch_image(monkeypatch, blocked=False):
    from creative_agent import image_qa, image_tools
    from tests._fakes import noop_async

    calls: list[dict] = []
    downloads: list[str] = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        return _image_response(blocked=blocked)

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    monkeypatch.setattr(image_tools, "_get_genai_client", lambda: client)
    monkeypatch.setattr(image_tools.asyncio, "sleep", noop_async)
    monkeypatch.setattr(image_tools.config, "GCS_BUCKET_NAME", BUCKET)

    def fake_download(bucket, obj):
        downloads.append(obj)
        return b"bytes:" + obj.encode()

    monkeypatch.setattr(image_tools, "_download_blob", fake_download)
    monkeypatch.setattr(image_qa, "_get_qa_client", lambda: "qa")
    monkeypatch.setattr(
        image_qa,
        "inspect_image",
        lambda *a, **k: image_qa.ImageQAResult(
            product_visible=True,
            motif_visible=True,
            brand_cue_visible=True,
            text_expected=False,
            text_exact=True,
            text_legible=True,
            gibberish_text=False,
            unrequested_logos=False,
            artifacts=False,
            unsafe=False,
            issues=[],
            person_cast=True,
            person_likeness=True,
        ),
    )
    return calls, downloads


def test_render_variant_uses_run_inputs_and_the_person(monkeypatch):
    calls, downloads = _patch_image(monkeypatch)
    state = creative_state()
    state["visual_aspect_ratio"] = "4:5"
    state["rating_strictness"] = ["unwanted_logo"]
    state["reference_image_uri"] = ""
    state["reference_images"] = [
        {"uri": f"gs://{BUCKET}/refs/product.png", "role": "product"},
        # A person photo smuggled in as a "style" reference is never used.
        {"uri": PHOTO_A2, "role": "style"},
    ]
    concept = vr.find_concept(state, CASTABLE)
    out = asyncio.run(vr.render_variant(state, concept, PHOTO_A))
    assert out.status == "done" and out.image_bytes == b"img"
    assert out.qa is not None and out.qa["passed"] is True
    assert downloads == ["refs/product.png", f"person-refs/{pr.slug_for(A)}/me.jpg"]
    (call,) = calls
    image_config = call["config"].image_config
    assert image_config.aspect_ratio == "4:5"
    assert image_config.person_generation == "ALLOW_ADULT"
    text = call["contents"][0]
    assert "Reference image 1 (product)" in text
    assert "Reference image 2 (person)" in text
    assert "No logos, brand marks or trademarks except those of" in text
    assert "The hero is the person in the person reference image." in text


def test_render_variant_blocked_is_rejected_without_fallback(monkeypatch):
    calls, _ = _patch_image(monkeypatch, blocked=True)
    state = creative_state()
    concept = vr.find_concept(state, CASTABLE)
    out = asyncio.run(vr.render_variant(state, concept, PHOTO_A))
    assert out.status == "rejected" and out.reason == "image_safety"
    assert len(calls) == 1  # no person-less render


def test_render_variant_photo_unavailable(monkeypatch):
    calls, _ = _patch_image(monkeypatch)
    from creative_agent import image_tools

    monkeypatch.setattr(image_tools.config, "GCS_BUCKET_NAME", "another-bucket")
    state = creative_state()
    out = asyncio.run(
        vr.render_variant(state, vr.find_concept(state, CASTABLE), PHOTO_A)
    )
    assert out.status == "failed" and out.reason == "photo_unavailable"
    assert calls == []
