"""Cast creatives as experiment arms: the consent check at create and the
person-reference revoke cascade that stops live experiments (offline)."""

from __future__ import annotations

import asyncio
import datetime as dt

import httpx
from fastapi import FastAPI

from runserver import experiments as ex
from runserver import person_refs as pr
from runserver.person_refs_store import InMemoryPersonRefsStore
from tests.test_experiments_api import A, B, Harness, _fixture_state, run

CAST = "The Golden Golf Cart Gig"
CONSENT = "consentAAAA1"


def _consents() -> InMemoryPersonRefsStore:
    store = InMemoryPersonRefsStore()
    pr.configure(store=store, revoke_hooks=[ex.stop_experiments_for_consent])
    return store


async def _consent(store, cid=CONSENT, owner=A, revoked=False):
    now = dt.datetime.now(dt.UTC)
    await store.put(
        {
            "consent_id": cid,
            "owner_user": owner,
            "photo_uri": f"gs://bkt/person-refs/x/{cid}.jpg",
            "label": "Sam",
            "subject": "self",
            "adult_attested": True,
            "allow_public_share": False,
            "consent_text_version": pr.CONSENT_TEXT_VERSION,
            "created_at": now,
            "revoked_at": now if revoked else None,
            "person_renders": [],
        }
    )


def _cast_state(cid: str | None = CONSENT) -> dict:
    state = _fixture_state()
    record: dict = {"gcs_uri": "gs://bkt/x.png", "cast": True}
    if cid is not None:
        record["consent_id"] = cid
    state["generated_images"] = {CAST: record}
    return state


def test_cast_creative_with_an_active_consent_is_an_arm():
    async def go():
        store = _consents()
        await _consent(store)
        h = Harness()
        await h.session(state=_cast_state())
        r = await h.create(indices=(0, 1))
        assert r.status_code == 201, r.text
        arms = h.store.rows[r.json()["experimentId"]]["arms"]
        assert arms[0]["consentId"] == CONSENT
        assert "consentId" not in arms[1]
        detail = await h.client.get(f"/experiments/{A}/{r.json()['experimentId']}")
        assert detail.json()["arms"][0]["consentId"] == CONSENT

    run(go)


def _refused(revoked=False, owner=A, cid: str | None = CONSENT):
    async def go():
        store = _consents()
        await _consent(store, owner=owner, revoked=revoked)
        h = Harness()
        await h.session(state=_cast_state(cid))
        r = await h.create(indices=(0, 1))
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["reason"] == "consent_not_active"
        assert r.json()["detail"]["message"] == (
            f"'{CAST}' features a person whose consent is no longer active"
        )
        assert not h.store.rows and not h.writer.files
        assert not h.deployer.endpoints

    run(go)


def test_revoked_consent_is_refused():
    _refused(revoked=True)


def test_another_owners_consent_is_refused():
    _refused(owner=B)


def test_missing_consent_id_is_refused():
    _refused(cid=None)


def test_unknown_consent_is_refused():
    _refused(cid="consentZZZZ9")


def test_consent_store_error_is_503_before_anything_is_written(monkeypatch):
    async def go():
        store = _consents()

        async def boom(*_a):
            raise RuntimeError("bq down")

        monkeypatch.setattr(store, "active_for", boom)
        h = Harness()
        await h.session(state=_cast_state())
        r = await h.create(indices=(0, 1))
        assert r.status_code == 503
        assert r.json()["detail"]["reason"] == "consent_unavailable"
        assert not h.store.rows and not h.writer.files
        assert not h.deployer.endpoints

    run(go)


def test_person_image_is_still_refused_with_an_active_consent():
    async def go():
        store = _consents()
        await _consent(store)
        h = Harness()
        state = _cast_state()
        state["agent_output_dir"] = "variants"
        await h.session(state=state)
        r = await h.create(indices=(0, 1))
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "person_image"

    run(go)


# --- revoke cascade ---------------------------------------------------------------


async def _experiment(h: Harness, store, user=A, sid="s1", cid=CONSENT) -> str:
    await _consent(store, cid=cid, owner=user)
    await h.session(user=user, sid=sid, state=_cast_state(cid))
    r = await h.create(user=user, sid=sid, indices=(0, 1))
    assert r.status_code == 201, r.text
    return r.json()["experimentId"]


def test_revoke_hook_stops_a_live_experiment_with_the_consent():
    async def go():
        store = _consents()
        h = Harness()
        eid = await _experiment(h, store)
        await ex.drain()
        endpoint = h.store.rows[eid]["endpoint_id"]
        await h.client.post(f"/experiments/{A}/{eid}/traffic", json={"episodes": 5})
        assert h.store.rows[eid]["status"] == "running_traffic"
        record = {"owner_user": A, "consent_id": CONSENT}
        await ex.stop_experiments_for_consent(record)
        await ex.drain()
        assert h.store.rows[eid]["status"] == "stopped"
        assert endpoint not in h.deployer.endpoints
        # Idempotent: a repeated revoke leaves the stopped experiment alone.
        await ex.stop_experiments_for_consent(record)
        assert h.deployer.teardowns == [endpoint]

    run(go)


def test_revoke_hook_stops_a_deploying_experiment():
    async def go():
        store = _consents()
        h = Harness(wait=0.0)
        h.deployer.gate = asyncio.Event()
        eid = await _experiment(h, store)
        await asyncio.sleep(0)
        await ex.stop_experiments_for_consent({"owner_user": A, "consent_id": CONSENT})
        h.deployer.gate.set()
        await ex.drain()
        assert h.store.rows[eid]["status"] == "stopped"
        assert not h.deployer.endpoints

    run(go)


def test_revoke_hook_ignores_other_consents_and_owners():
    async def go():
        store = _consents()
        h = Harness()
        mine = await _experiment(h, store)
        theirs = await _experiment(h, store, user=B, sid="s2", cid="consentBBBB2")
        await ex.drain()
        await ex.stop_experiments_for_consent(
            {"owner_user": A, "consent_id": "consentOTHER"}
        )
        assert h.store.rows[mine]["status"] == "ready"
        # Only the record owner's experiments are searched, even for B's id.
        await ex.stop_experiments_for_consent(
            {"owner_user": A, "consent_id": "consentBBBB2"}
        )
        await ex.stop_experiments_for_consent({"owner_user": A, "consent_id": CONSENT})
        await ex.drain()
        assert h.store.rows[mine]["status"] == "stopped"
        assert h.store.rows[theirs]["status"] == "ready"
        await ex.stop_experiments_for_consent({"owner_user": "", "consent_id": None})

    run(go)


def _revoke_client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(pr.router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


def test_person_ref_revoke_stops_the_experiment():
    async def go():
        store = _consents()
        h = Harness()
        eid = await _experiment(h, store)
        await ex.drain()
        r = await _revoke_client().delete(f"/person-refs/{A}/{CONSENT}")
        assert r.status_code == 204, r.text
        await ex.drain()
        assert h.store.rows[eid]["status"] == "stopped"

    run(go)


def test_hook_failure_makes_the_revoke_502_and_the_retry_finishes(monkeypatch):
    async def go():
        store = _consents()
        h = Harness()
        eid = await _experiment(h, store)
        await ex.drain()
        real = h.store.list_for_user

        async def boom(*_a):
            raise RuntimeError("bq down")

        monkeypatch.setattr(h.store, "list_for_user", boom)
        client = _revoke_client()
        r = await client.delete(f"/person-refs/{A}/{CONSENT}")
        assert r.status_code == 502
        assert r.json()["detail"]["reason"] == "revoke_incomplete"
        assert h.store.rows[eid]["status"] == "ready"
        monkeypatch.setattr(h.store, "list_for_user", real)
        assert (await client.delete(f"/person-refs/{A}/{CONSENT}")).status_code == 204
        await ex.drain()
        assert h.store.rows[eid]["status"] == "stopped"

    run(go)


def test_failed_teardown_makes_the_hook_raise(monkeypatch):
    async def go():
        store = _consents()
        h = Harness()
        eid = await _experiment(h, store)
        await ex.drain()

        async def broken(_row):
            raise RuntimeError("vertex down")

        monkeypatch.setattr(h.deployer, "teardown", broken)
        record = {"owner_user": A, "consent_id": CONSENT}
        try:
            await ex.stop_experiments_for_consent(record)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a failed teardown must fail the hook")
        assert h.store.rows[eid]["status"] == "stopping"
        monkeypatch.undo()
        await ex.stop_experiments_for_consent(record)  # the retry finishes it
        await ex.drain()
        assert h.store.rows[eid]["status"] == "stopped"

    run(go)
