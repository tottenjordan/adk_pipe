"""runserver/person_refs.py: the owner-scoped /person-refs consent routes (offline).

``InMemoryPersonRefsStore`` + a fake GCS client, driven over
``httpx.ASGITransport`` with the real ``UserAuthzMiddleware`` in front.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from runserver import person_refs as pr
from runserver.authz import AuthzMode, UserAuthzMiddleware
from runserver.person_refs_store import InMemoryPersonRefsStore

A = "alice.smith@example.com"
B = "bob@example.com"
BUCKET = "tt-bucket"
SLUG_A = "alice_smith_example_com-7dcd3a39ad"
SLUG_B = "bob_example_com-5ff860bf11"
PROXY = {"Authorization": "Bearer proxy"}
PHOTO_A = f"gs://{BUCKET}/person-refs/{SLUG_A}/me.jpg"


# --- fake GCS ---------------------------------------------------------------------


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str, meta: dict | None = None):
        self.bucket, self.name = bucket, name
        meta = meta or {}
        self.size = meta.get("size")
        self.content_type = meta.get("content_type")
        self.metadata = meta.get("metadata")

    def delete(self):
        from google.api_core.exceptions import NotFound

        self.bucket.gcs.ops.append(("delete", self.bucket.name, self.name))
        if self.bucket.gcs.fail_delete:
            raise RuntimeError("delete failed")
        if self.name not in self.bucket.objects:
            raise NotFound("gone")
        del self.bucket.objects[self.name]


class FakeBucket:
    def __init__(self, gcs: FakeGCS, name: str):
        self.gcs, self.name = gcs, name
        self.objects = gcs.objects.setdefault(name, {})

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def get_blob(self, name: str):
        self.gcs.ops.append(("head", self.name, name))
        if self.gcs.fail_head:
            raise RuntimeError("head failed")
        meta = self.objects.get(name)
        return FakeBlob(self, name, meta) if meta is not None else None


class FakeGCS:
    def __init__(self):
        self.objects: dict[str, dict[str, dict]] = {}
        self.ops: list[tuple] = []
        self.fail_head = False
        self.fail_delete = False

    def add(
        self,
        uri: str,
        size: int = 1000,
        content_type: str = "image/jpeg",
        metadata: dict | None = None,
    ):
        bucket, _, path = uri.removeprefix("gs://").partition("/")
        self.objects.setdefault(bucket, {})[path] = {
            "size": size,
            "content_type": content_type,
            "metadata": metadata,
        }

    def has(self, uri: str) -> bool:
        bucket, _, path = uri.removeprefix("gs://").partition("/")
        return path in self.objects.get(bucket, {})

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self, name)


# --- harness ----------------------------------------------------------------------


class Harness:
    def __init__(self, mode=AuthzMode.TRUST_CLIENT, hooks=None):
        from google.adk.sessions import InMemorySessionService

        self.store = InMemoryPersonRefsStore()
        self.gcs = FakeGCS()
        self.gcs.add(PHOTO_A)
        self.svc = InMemorySessionService()
        pr.configure(
            store=self.store,
            gcs_client=self.gcs,
            bucket=BUCKET,
            revoke_hooks=hooks,
            session_service=self.svc,
        )
        app = FastAPI()
        app.include_router(pr.router)
        app.add_middleware(
            UserAuthzMiddleware, mode=mode, caller_ok=lambda a: a == "Bearer proxy"
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    async def create(self, user=A, headers=None, **over):
        body = {
            "photo_uri": PHOTO_A,
            "label": "Alice",
            "subject": "self",
            "adult_attested": True,
            "allow_public_share": False,
            "consent_text_version": pr.CONSENT_TEXT_VERSION,
        }
        body.update(over)
        return await self.client.post(
            f"/person-refs/{user}", json=body, headers=headers or {}
        )


def run(coro_fn):
    return asyncio.run(coro_fn())


# --- pure helpers -----------------------------------------------------------------


SLUG_GOLDEN = json.loads(
    (Path(__file__).resolve().parent / "fixtures/person_slugs.json").read_text()
)


@pytest.mark.parametrize("case", SLUG_GOLDEN, ids=lambda c: c["email"])
def test_slug_for_matches_the_shared_golden(case):
    # The same fixture drives frontend/src/__tests__/gcs-route-person.test.ts, so
    # the api and the /api/gcs owner check can't drift apart.
    assert pr.slug_for(case["email"]) == case["slug"]


def test_slug_for_is_readable_and_collision_free():
    assert pr.slug_for("admin@jordantotten.altostrat.com").startswith(
        "admin_jordantotten_altostrat_com-"
    )
    # the readable part alone collides; the hash suffix keeps them apart
    assert pr.slug_for("a.b@x.com") != pr.slug_for("a_b@x.com")
    assert pr.slug_for(" Alice.Smith@Example.com ") == SLUG_A
    assert pr.slug_for(B) == SLUG_B


def test_photo_path_only_under_the_owner_prefix():
    ok = pr.photo_path
    assert ok(PHOTO_A, A, BUCKET) == f"person-refs/{SLUG_A}/me.jpg"
    assert ok(PHOTO_A.replace(".jpg", ".WEBP"), A, BUCKET) is not None
    for bad in (
        PHOTO_A.replace(".jpg", ".gif"),  # not an allowed image type
        f"gs://other/person-refs/{SLUG_A}/me.jpg",  # another bucket
        f"gs://{BUCKET}/person-refs/{SLUG_B}/me.jpg",  # another owner
        f"gs://{BUCKET}/person-refs/{SLUG_A}/sub/me.jpg",  # nested
        f"gs://{BUCKET}/person-refs/{SLUG_A}/../bob_example_com/me.jpg",
        f"gs://{BUCKET}/person-refs/{SLUG_A}/.jpg",
        f"gs://{BUCKET}/x/person-refs/{SLUG_A}/me.jpg",
        f"https://{BUCKET}/person-refs/{SLUG_A}/me.jpg",
        None,
        42,
    ):
        assert ok(bad, A, BUCKET) is None, bad
    assert ok(PHOTO_A, A, None) is None


# --- POST -------------------------------------------------------------------------


def test_create_records_consent_and_returns_it():
    async def go():
        h = Harness()
        r = await h.create(allow_public_share=True, label="  Alice  ")
        assert r.status_code == 200, r.text
        got = r.json()
        assert pr.CONSENT_ID_RE.match(got["consent_id"])
        assert got["photo_uri"] == PHOTO_A
        assert got["label"] == "Alice"
        assert got["subject"] == "self"
        assert got["allow_public_share"] is True
        assert got["consent_text_version"] == pr.CONSENT_TEXT_VERSION
        assert "owner_user" not in got and "person_renders" not in got
        row = await h.store.active_for(got["consent_id"], A)
        assert row is not None and row["owner_user"] == A
        assert row["adult_attested"] is True and row["person_renders"] == []
        assert ("head", BUCKET, f"person-refs/{SLUG_A}/me.jpg") in h.gcs.ops

    run(go)


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"photo_uri": f"gs://{BUCKET}/person-refs/{SLUG_B}/me.jpg"}, None),
        ({"photo_uri": f"gs://other/person-refs/{SLUG_A}/me.jpg"}, None),
        ({"photo_uri": f"gs://{BUCKET}/person-refs/{SLUG_A}/me.gif"}, None),
        ({"photo_uri": 3}, None),
        ({"adult_attested": False}, "adult_attestation_required"),
        ({"adult_attested": "yes"}, "adult_attestation_required"),
        ({"consent_text_version": "2020-01-01"}, "stale_consent_text"),
        ({"consent_text_version": None}, "stale_consent_text"),
        ({"subject": "celebrity"}, "invalid_subject"),
        ({"label": ""}, "invalid_label"),
        ({"label": "x" * 81}, "invalid_label"),
        ({"allow_public_share": "no"}, "invalid_allow_public_share"),
    ],
)
def test_create_validates_body(over, reason):
    async def go():
        h = Harness()
        r = await h.create(**over)
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == (reason or "invalid_photo_uri")
        assert h.store.rows == {}

    run(go)


def test_another_users_prefix_is_rejected_before_any_gcs_read():
    async def go():
        h = Harness()
        foreign = f"gs://{BUCKET}/person-refs/{SLUG_A}/me.jpg"
        r = await h.create(user=B, photo_uri=foreign)
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "invalid_photo_uri"
        assert h.gcs.ops == []

    run(go)


@pytest.mark.parametrize(
    "setup",
    ["missing", "too_big", "not_image", "head_error"],
)
def test_unreadable_photo_is_400(setup):
    async def go():
        h = Harness()
        if setup == "missing":
            h.gcs.objects[BUCKET].clear()
        elif setup == "too_big":
            h.gcs.add(PHOTO_A, size=pr.MAX_PHOTO_BYTES + 1)
        elif setup == "not_image":
            h.gcs.add(PHOTO_A, content_type="application/pdf")
        else:
            h.gcs.fail_head = True
        r = await h.create()
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "photo_unreadable"
        assert h.store.rows == {}

    run(go)


def test_same_photo_cannot_be_registered_twice():
    async def go():
        h = Harness()
        assert (await h.create()).status_code == 200
        r = await h.create(label="Again")
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "already_registered"

    run(go)


def test_active_cap_is_429(monkeypatch):
    async def go():
        monkeypatch.setattr(pr, "MAX_ACTIVE_REFS", 1)
        h = Harness()
        assert (await h.create()).status_code == 200
        other = PHOTO_A.replace("me.jpg", "two.png")
        h.gcs.add(other, content_type="image/png")
        r = await h.create(photo_uri=other)
        assert r.status_code == 429
        assert r.json()["detail"]["reason"] == "too_many_person_refs"

    run(go)


def test_unconfigured_bucket_is_503():
    async def go():
        h = Harness()
        pr.configure(store=h.store, gcs_client=h.gcs, bucket="")
        pr._BUCKET = None
        r = await h.create()
        assert r.status_code == 503
        assert r.json()["detail"]["reason"] == "person_refs_unconfigured"

    run(go)


# --- GET --------------------------------------------------------------------------


def test_list_is_owner_only_and_carries_the_prefix():
    async def go():
        h = Harness()
        first = (await h.create()).json()["consent_id"]
        r = await h.client.get(f"/person-refs/{A}")
        assert r.status_code == 200
        body = r.json()
        assert [p["consent_id"] for p in body["person_refs"]] == [first]
        assert body["prefix"] == f"gs://{BUCKET}/person-refs/{SLUG_A}/"
        assert body["consent_text_version"] == pr.CONSENT_TEXT_VERSION
        other = (await h.client.get(f"/person-refs/{B}")).json()
        assert other["person_refs"] == []
        assert other["prefix"] == f"gs://{BUCKET}/person-refs/{SLUG_B}/"

    run(go)


# --- DELETE -----------------------------------------------------------------------


def test_revoke_deletes_photo_runs_hooks_and_hides_record():
    seen: list[str] = []

    async def async_hook(row):
        seen.append(f"async:{row['consent_id']}")

    def sync_hook(row):
        seen.append(f"sync:{row['consent_id']}")

    async def go():
        h = Harness(hooks=[async_hook, sync_hook])
        cid = (await h.create()).json()["consent_id"]
        r = await h.client.delete(f"/person-refs/{A}/{cid}")
        assert r.status_code == 204
        assert not h.gcs.has(PHOTO_A)
        assert seen == [f"async:{cid}", f"sync:{cid}"]
        assert await h.store.active_for(cid, A) is None
        assert (await h.client.get(f"/person-refs/{A}")).json()["person_refs"] == []
        # idempotent: a repeat finishes cleanly (the photo is already gone)
        r = await h.client.delete(f"/person-refs/{A}/{cid}")
        assert r.status_code == 204

    run(go)


def test_cross_owner_unknown_or_malformed_revoke_is_404():
    async def go():
        h = Harness()
        cid = (await h.create()).json()["consent_id"]
        r = await h.client.delete(f"/person-refs/{B}/{cid}")
        assert r.status_code == 404
        assert h.gcs.has(PHOTO_A)
        assert await h.store.active_for(cid, A) is not None
        r = await h.client.delete(f"/person-refs/{A}/{'x' * 16}")
        assert r.status_code == 404
        r = await h.client.delete(f"/person-refs/{A}/short")
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "person_ref_not_found"

    run(go)


def test_revoke_cleanup_failure_is_502_and_retryable():
    calls = []

    def flaky_hook(row):
        calls.append(row["consent_id"])
        if len(calls) == 1:
            raise RuntimeError("cascade failed")

    async def go():
        h = Harness(hooks=[flaky_hook])
        cid = (await h.create()).json()["consent_id"]
        r = await h.client.delete(f"/person-refs/{A}/{cid}")
        assert r.status_code == 502
        assert r.json()["detail"]["reason"] == "revoke_incomplete"
        # revoked already (never usable again), the retry finishes the cleanup
        assert await h.store.active_for(cid, A) is None
        assert (await h.client.delete(f"/person-refs/{A}/{cid}")).status_code == 204
        assert not h.gcs.has(PHOTO_A)
        assert calls == [cid, cid]

    run(go)


def test_photo_delete_failure_is_502():
    async def go():
        h = Harness()
        cid = (await h.create()).json()["consent_id"]
        h.gcs.fail_delete = True
        r = await h.client.delete(f"/person-refs/{A}/{cid}")
        assert r.status_code == 502
        assert r.json()["detail"]["reason"] == "revoke_incomplete"
        h.gcs.fail_delete = False
        assert (await h.client.delete(f"/person-refs/{A}/{cid}")).status_code == 204
        assert not h.gcs.has(PHOTO_A)

    run(go)


# --- revoke cascade: renders made with the photo ------------------------------------


RUN = f"gs://{BUCKET}/run1/creative_output"


def _render(cid: str, name: str = "c1.png") -> str:
    return f"{RUN}/{name}"


def _variant(cid: str, name: str = "k1.png") -> str:
    return f"{RUN}/variants/{SLUG_A}/concept/{name}"


def test_render_path_only_for_run_outputs_in_the_bucket():
    assert pr.render_path(f"gs://{BUCKET}/run/out/c.png", BUCKET) == "run/out/c.png"
    assert pr.render_path(f"gs://{BUCKET}/run/out/variants/s/c/k.png", BUCKET) == (
        "run/out/variants/s/c/k.png"
    )
    for bad in (
        "gs://other/run/out/c.png",
        f"gs://{BUCKET}/shares/tok/0.png",
        f"gs://{BUCKET}/person-refs/{SLUG_A}/me.jpg",
        f"gs://{BUCKET}/run/../person-refs/x.jpg",
        f"gs://{BUCKET}/",
        "https://x/y.png",
        None,
        42,
    ):
        assert pr.render_path(bad, BUCKET) is None, bad
    assert pr.render_path(f"gs://{BUCKET}/run/c.png", None) is None


def test_record_renders_appends_deduped_owner_only():
    async def go():
        h = Harness()
        cid = (await h.create()).json()["consent_id"]
        ok = await pr.record_renders(
            A,
            cid,
            [
                _render(cid),
                _render(cid),
                f"gs://{BUCKET}/shares/t/0.png",  # never a share copy
                "gs://other/x.png",  # never outside the bucket
            ],
        )
        assert ok is True
        assert (await h.store.get(cid))["person_renders"] == [_render(cid)]
        assert await pr.record_renders(A, cid, [_variant(cid)]) is True
        assert (await h.store.get(cid))["person_renders"] == [
            _render(cid),
            _variant(cid),
        ]
        # someone else's consent: refused, nothing recorded
        assert await pr.record_renders(B, cid, [_render(cid, "b.png")]) is False
        assert await pr.record_renders(A, "not valid!", [_render(cid)]) is False
        assert len((await h.store.get(cid))["person_renders"]) == 2

    run(go)


def test_record_renders_after_revoke_deletes_them_at_once():
    async def go():
        h = Harness()
        cid = (await h.create()).json()["consent_id"]
        assert (await h.client.delete(f"/person-refs/{A}/{cid}")).status_code == 204
        late = _variant(cid, "late.png")
        h.gcs.add(late, metadata={"consent_id": cid})
        assert await pr.record_renders(A, cid, [late]) is False
        assert not h.gcs.has(late)
        assert (await h.store.get(cid))["person_renders"] == [late]

    run(go)


def test_revoke_deletes_shares_then_renders_then_photo():
    order: list[str] = []

    def share_hook(row):
        order.append("shares")
        # the hook sees the record re-read after the revoke (renders included)
        assert len(row["person_renders"]) == 4

    async def go():
        h = Harness(hooks=[share_hook])
        cid = (await h.create()).json()["consent_id"]
        base, variant = _render(cid), _variant(cid)
        foreign = _render(cid, "uncast.png")  # overwritten by an uncast render
        missing = _render(cid, "gone.png")
        h.gcs.add(base, metadata={"consent_id": cid})
        h.gcs.add(variant, metadata={"consent_id": cid})
        h.gcs.add(foreign, metadata=None)
        await pr.record_renders(A, cid, [base, variant, foreign, missing])
        r = await h.client.delete(f"/person-refs/{A}/{cid}")
        assert r.status_code == 204
        assert order == ["shares"]
        assert not h.gcs.has(base) and not h.gcs.has(variant)
        # an object no longer carrying this consent's metadata is never deleted
        assert h.gcs.has(foreign)
        assert not h.gcs.has(PHOTO_A)
        deletes = [op[2] for op in h.gcs.ops if op[0] == "delete"]
        assert deletes[-1] == PHOTO_A.removeprefix(f"gs://{BUCKET}/")
        # idempotent repeat
        assert (await h.client.delete(f"/person-refs/{A}/{cid}")).status_code == 204

    run(go)


def test_record_session_marks_the_run_on_the_consent():
    async def go():
        h = Harness()
        cid = (await h.create()).json()["consent_id"]
        assert await pr.record_session(A, cid, "creative_agent", "s-1") is True
        assert await pr.record_session(A, cid, "creative_agent", "s-1") is True
        assert (await h.store.get(cid))["person_renders"] == [
            "session:creative_agent/s-1"
        ]
        # malformed app / session id, someone else's consent: refused
        for args in (
            (A, cid, "bad app", "s"),
            (A, cid, "creative_agent", "a/b"),
            (A, cid, "creative_agent", ""),
            (B, cid, "creative_agent", "s-2"),
            (A, "not valid!", "creative_agent", "s-2"),
        ):
            assert await pr.record_session(*args) is False, args
        assert len((await h.store.get(cid))["person_renders"]) == 1
        # a session marker is never mistaken for an object to delete
        assert pr.render_path("session:creative_agent/s-1", BUCKET) is None

    run(go)


def test_revoke_deletes_cast_renders_and_variants_of_recorded_sessions():
    async def go():
        h = Harness()
        cid = (await h.create()).json()["consent_id"]
        cast, plain = _render(cid, "cast.png"), _render(cid, "plain.png")
        variant = _variant(cid, "v.png")
        h.gcs.add(cast, metadata={"consent_id": cid})
        h.gcs.add(plain, metadata=None)
        h.gcs.add(variant, metadata={"consent_id": cid})
        # the run died mid-segment: nothing was recorded but the session marker
        await h.svc.create_session(
            app_name="creative_agent",
            user_id=A,
            session_id="s-1",
            state={
                "generated_images": {
                    "c": {"gcs_uri": cast, "cast": True, "consent_id": cid},
                    "p": {"gcs_uri": plain, "cast": False},
                    "x": "junk",
                },
                "person_variants": {
                    "p": {"k1": {"consent_id": cid, "gcs_uri": variant}},
                    "q": {"k2": {"consent_id": "other-1234", "gcs_uri": plain}},
                },
            },
        )
        await pr.record_session(A, cid, "creative_agent", "s-1")
        await pr.record_session(A, cid, "creative_agent", "gone-1")  # deleted run
        r = await h.client.delete(f"/person-refs/{A}/{cid}")
        assert r.status_code == 204, r.text
        assert not h.gcs.has(cast) and not h.gcs.has(variant)
        assert h.gcs.has(plain)
        assert not h.gcs.has(PHOTO_A)
        assert (await h.client.delete(f"/person-refs/{A}/{cid}")).status_code == 204

    run(go)


def test_session_read_failure_is_502_and_retryable(monkeypatch):
    async def go():
        h = Harness()
        cid = (await h.create()).json()["consent_id"]
        await pr.record_session(A, cid, "creative_agent", "s-1")
        real = h.svc.get_session

        async def boom(**kw):
            raise RuntimeError("sessions down")

        monkeypatch.setattr(h.svc, "get_session", boom)
        r = await h.client.delete(f"/person-refs/{A}/{cid}")
        assert r.status_code == 502
        assert h.gcs.has(PHOTO_A)
        monkeypatch.setattr(h.svc, "get_session", real)
        assert (await h.client.delete(f"/person-refs/{A}/{cid}")).status_code == 204
        assert not h.gcs.has(PHOTO_A)

    run(go)


def test_launcher_registers_the_share_cascade():
    # Importing async_app builds the whole ADK server, so check the wiring textually.
    src = (Path(__file__).resolve().parents[1] / "deployment/async_app.py").read_text()
    assert "revoke_hooks=[shares.revoke_shares_for_consent]" in src
    call = src.split("person_refs.configure(")[1].split("\n)")[0]
    assert "session_service=session_service" in call


def test_render_delete_failure_is_502_and_the_retry_finishes():
    async def go():
        h = Harness()
        cid = (await h.create()).json()["consent_id"]
        base = _render(cid)
        h.gcs.add(base, metadata={"consent_id": cid})
        await pr.record_renders(A, cid, [base])
        h.gcs.fail_delete = True
        r = await h.client.delete(f"/person-refs/{A}/{cid}")
        assert r.status_code == 502
        assert r.json()["detail"]["reason"] == "revoke_incomplete"
        assert h.gcs.has(base) and h.gcs.has(PHOTO_A)
        h.gcs.fail_delete = False
        assert (await h.client.delete(f"/person-refs/{A}/{cid}")).status_code == 204
        assert not h.gcs.has(base) and not h.gcs.has(PHOTO_A)

    run(go)


# --- helpers for later PRs --------------------------------------------------------


def test_active_consent_helper():
    async def go():
        h = Harness()
        cid = (await h.create()).json()["consent_id"]
        assert (await pr.active_consent(A, cid))["photo_uri"] == PHOTO_A
        assert await pr.active_consent(B, cid) is None
        assert await pr.active_consent(A, "not a valid id!") is None
        await h.client.delete(f"/person-refs/{A}/{cid}")
        assert await pr.active_consent(A, cid) is None

    run(go)


# --- authz ------------------------------------------------------------------------


def test_enforce_mode_requires_trusted_matching_user():
    async def go():
        h = Harness(mode=AuthzMode.ENFORCE)
        ok = {**PROXY, "X-TT-User": A}
        assert (await h.create()).status_code == 401
        assert (await h.create(headers={"X-TT-User": A})).status_code == 401
        assert (await h.create(user=B, headers=ok)).status_code == 403
        assert (await h.client.get(f"/person-refs/{A}")).status_code == 401
        assert (await h.client.get(f"/person-refs/{B}", headers=ok)).status_code == 403
        assert (
            await h.client.delete(f"/person-refs/{B}/{'x' * 16}", headers=ok)
        ).status_code == 403
        r = await h.create(headers=ok)
        assert r.status_code == 200
        cid = r.json()["consent_id"]
        assert (await h.client.get(f"/person-refs/{A}", headers=ok)).status_code == 200
        r = await h.client.delete(f"/person-refs/{A}/{cid}", headers=ok)
        assert r.status_code == 204

    run(go)
