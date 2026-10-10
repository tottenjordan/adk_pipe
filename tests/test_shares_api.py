"""runserver/shares.py: the owner-scoped /shares REST routes (offline).

In-memory session service + ``InMemorySharesStore`` + a fake GCS client, driven
over ``httpx.ASGITransport`` with the real ``UserAuthzMiddleware`` in front.
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest
from fastapi import FastAPI
from google.adk.sessions import InMemorySessionService

from runserver import person_refs as pr
from runserver import shares as sh
from runserver.authz import AuthzMode, UserAuthzMiddleware, install_ownership_handler
from runserver.person_refs_store import InMemoryPersonRefsStore
from runserver.shares_store import InMemorySharesStore
from tests._creative_fixtures import (
    BUCKET,
    CONCEPTS,
    creative_report,
    creative_state,
    image_uri,
)

A = "alice@example.com"
B = "bob@example.com"
APP = "creative_agent"
PROXY = {"Authorization": "Bearer proxy"}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


# --- fake GCS ---------------------------------------------------------------------


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str):
        self.bucket, self.name = bucket, name
        self.cache_control: str | None = None
        self.content_type: str | None = None
        obj = bucket.objects.get(name) or {}
        self.metadata: dict | None = (
            dict(obj["metadata"]) if obj.get("metadata") else None
        )

    def upload_from_string(self, data, content_type=None):
        self.bucket.gcs.ops.append(("upload", self.bucket.name, self.name))
        if self.bucket.gcs.fail_on("upload", self.name):
            raise RuntimeError("upload failed")
        self.content_type = content_type
        self.bucket.objects[self.name] = {
            "data": data,
            "content_type": content_type,
            "cache_control": self.cache_control,
        }

    def patch(self):
        self.bucket.gcs.ops.append(("patch", self.bucket.name, self.name))
        obj = self.bucket.objects[self.name]
        obj["cache_control"] = self.cache_control
        # GCS semantics: a metadata key patched to None is removed.
        merged = {**(obj.get("metadata") or {}), **(self.metadata or {})}
        obj["metadata"] = {k: v for k, v in merged.items() if v is not None} or None

    def delete(self):
        self.bucket.gcs.ops.append(("delete", self.bucket.name, self.name))
        self.bucket.objects.pop(self.name, None)


class FakeBucket:
    def __init__(self, gcs: FakeGCS, name: str):
        self.gcs, self.name = gcs, name
        self.objects = gcs.objects.setdefault(name, {})

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def get_blob(self, name: str):
        self.gcs.ops.append(("head", self.name, name))
        if self.gcs.fail_on("head", name):
            raise RuntimeError("head failed")
        return FakeBlob(self, name) if name in self.objects else None

    def copy_blob(self, blob: FakeBlob, destination_bucket: FakeBucket, new_name: str):
        self.gcs.ops.append(("copy", blob.name, destination_bucket.name, new_name))
        if self.gcs.fail_on("copy", new_name):
            raise RuntimeError("copy failed")
        src = self.objects[blob.name]
        destination_bucket.objects[new_name] = dict(src)
        return FakeBlob(destination_bucket, new_name)

    def list_blobs(self, prefix: str = ""):
        return [FakeBlob(self, n) for n in list(self.objects) if n.startswith(prefix)]


class FakeGCS:
    def __init__(self):
        self.objects: dict[str, dict[str, dict]] = {}
        self.ops: list[tuple] = []
        self.fail: tuple[str, str] | None = None  # (op, name substring)
        for i in range(len(CONCEPTS)):
            path = image_uri(i).removeprefix(f"gs://{BUCKET}/")
            self.objects.setdefault(BUCKET, {})[path] = {
                "data": b"png",
                "content_type": "image/png",
                "cache_control": None,
            }

    def set_metadata(self, i: int, metadata: dict | None):
        path = image_uri(i).removeprefix(f"gs://{BUCKET}/")
        self.objects[BUCKET][path]["metadata"] = metadata

    def fail_on(self, op: str, name: str) -> bool:
        return self.fail is not None and self.fail[0] == op and self.fail[1] in name

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self, name)

    def shared(self, token: str) -> dict[str, dict]:
        return {
            n: o
            for n, o in self.objects.get(BUCKET, {}).items()
            if n.startswith(f"shares/{token}/")
        }


# --- harness ----------------------------------------------------------------------


class Harness:
    def __init__(self, mode=AuthzMode.TRUST_CLIENT, base_url="https://share.example/"):
        self.svc = InMemorySessionService()
        self.store = InMemorySharesStore()
        self.gcs = FakeGCS()
        self.loads: list[str] = []
        self.consents = InMemoryPersonRefsStore()
        pr.configure(store=self.consents, gcs_client=self.gcs, bucket=BUCKET)

        def loader(uri: str):
            self.loads.append(uri)
            return creative_report()

        sh.configure(
            session_service=self.svc,
            store=self.store,
            gcs_client=self.gcs,
            bucket=BUCKET,
            share_base_url=base_url,
            report_loader=loader,
        )
        app = FastAPI()
        app.include_router(sh.router)
        install_ownership_handler(app)
        app.add_middleware(
            UserAuthzMiddleware, mode=mode, caller_ok=lambda a: a == "Bearer proxy"
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    async def session(self, user=A, sid="s1", state=None, app=APP):
        await self.svc.create_session(
            app_name=app,
            user_id=user,
            session_id=sid,
            state=creative_state() if state is None else state,
        )

    async def create(self, user=A, sid="s1", app=APP, headers=None, **body):
        return await self.client.post(
            f"/shares/{user}/{app}/{sid}", json=body, headers=headers or {}
        )


def run(coro_fn):
    return asyncio.run(coro_fn())


# --- POST -------------------------------------------------------------------------


def test_create_slate_share_copies_images_and_writes_snapshot():
    async def go():
        h = Harness()
        await h.session()
        r = await h.create()
        assert r.status_code == 200, r.text
        out = r.json()
        token = out["token"]
        assert TOKEN_RE.match(token)
        assert out["url"] == f"https://share.example/s/{token}"
        assert out["scope"] == "slate" and out["include_eval"] is False
        assert out["title"] == "Paul Reed Smith (PRS) × Powerball"
        assert out["concept_names"] == list(CONCEPTS)
        assert out["session_id"] == "s1" and out["app_name"] == APP
        assert out["created_at"]

        objs = h.gcs.shared(token)
        assert set(objs) == {f"shares/{token}/{i}.png" for i in range(4)} | {
            f"shares/{token}/snapshot.json"
        }
        for i in range(4):
            assert objs[f"shares/{token}/{i}.png"]["cache_control"] == (
                "private, max-age=300"
            )
        snap_obj = objs[f"shares/{token}/snapshot.json"]
        assert snap_obj["content_type"] == "application/json"
        assert snap_obj["cache_control"] == "no-store"
        snap = json.loads(snap_obj["data"])
        assert snap["version"] == 1 and snap["token"] == token
        assert snap["scope"] == "slate" and len(snap["creatives"]) == 4
        blob = snap_obj["data"]
        for secret in ("image_generation_prompt", "rationale", "s1", A, "gs://"):
            assert secret not in blob
        # every write stays under shares/<token>/
        writes = [op for op in h.gcs.ops if op[0] in ("copy", "upload", "patch")]
        for op in writes:
            assert op[-1].startswith(f"shares/{token}/")
        # copied from the generated_images sources, in creative order
        copies = [op for op in h.gcs.ops if op[0] == "copy"]
        assert [c[1] for c in copies] == [
            image_uri(i).removeprefix(f"gs://{BUCKET}/") for i in range(4)
        ]
        # recorded with the owner + session (never in the snapshot)
        row = await h.store.get(token)
        assert row["owner_user"] == A and row["session_id"] == "s1"
        assert row["revoked_at"] is None
        # no eval requested: the report is never read
        assert h.loads == []

    run(go)


def test_create_single_creative_with_eval():
    async def go():
        h = Harness()
        state = creative_state(with_report=False)
        state["eval_report_gcs_uri"] = (
            f"gs://{BUCKET}/creative_agent/run/creative_eval_report.json"
        )
        await h.session(state=state)
        r = await h.create(concept_names=["The Jackpot Reveal"], include_eval=True)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["scope"] == "creative" and out["include_eval"] is True
        assert out["title"] == "Looks Like a Jackpot. Sounds Like a Masterpiece."
        assert out["concept_names"] == ["The Jackpot Reveal"]
        snap = json.loads(
            h.gcs.shared(out["token"])[f"shares/{out['token']}/snapshot.json"]["data"]
        )
        assert snap["creatives"][0]["eval"]["visual"]["score"] == 0.633
        assert len(h.loads) == 1

    run(go)


def test_report_falls_back_to_state_and_ignores_foreign_uri():
    async def go():
        h = Harness()
        state = creative_state()
        state["eval_report_gcs_uri"] = "gs://other-bucket/x/creative_eval_report.json"
        await h.session(state=state)
        r = await h.create(include_eval=True)
        assert r.status_code == 200
        assert h.loads == []
        token = r.json()["token"]
        snap = json.loads(h.gcs.shared(token)[f"shares/{token}/snapshot.json"]["data"])
        assert snap["creatives"][0]["eval"]["copy"]["score"] == 0.85

    run(go)


def test_relative_url_without_base():
    async def go():
        h = Harness(base_url="")
        await h.session()
        out = (await h.create()).json()
        assert out["url"] == f"/s/{out['token']}"

    run(go)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"concept_names": "The Jackpot Reveal"}, "invalid_concept_names"),
        ({"concept_names": []}, "invalid_concept_names"),
        ({"concept_names": [1]}, "invalid_concept_names"),
        ({"concept_names": list(CONCEPTS) + ["x"]}, "invalid_concept_names"),
        ({"include_eval": "yes"}, "invalid_include_eval"),
        ({"concept_names": ["nope"]}, "unknown_concept"),
    ],
)
def test_create_validates_body(body, reason):
    async def go():
        h = Harness()
        await h.session()
        r = await h.create(**body)
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == reason
        assert h.gcs.ops == [] and h.store.rows == {}

    run(go)


def test_create_without_images_is_400():
    async def go():
        h = Harness()
        await h.session(state=creative_state(with_images=False))
        r = await h.create()
        assert r.status_code == 400 and r.json()["detail"]["reason"] == "no_images"

    run(go)


CONSENT = "consentAAAA1"


async def _consent(h, cid=CONSENT, owner=A, allow=True, revoked=False):
    await h.consents.put(
        {
            "consent_id": cid,
            "owner_user": owner,
            "photo_uri": f"gs://{BUCKET}/person-refs/x/{cid}.jpg",
            "label": "Sam",
            "subject": "self",
            "adult_attested": True,
            "allow_public_share": allow,
            "consent_text_version": pr.CONSENT_TEXT_VERSION,
            "created_at": sh.utcnow(),
            "revoked_at": sh.utcnow() if revoked else None,
            "person_renders": [],
        }
    )


def _cast_state(*names, cid=CONSENT):
    state = creative_state()
    for name in names:
        state["generated_images"][name]["cast"] = True
        state["generated_images"][name]["consent_id"] = cid
    return state


@pytest.mark.parametrize(
    "consent",
    [
        {"allow": False},  # no public-share consent
        {"allow": True, "revoked": True},  # revoked
        {"allow": True, "owner": B},  # someone else's consent
        None,  # unknown consent
    ],
)
def test_sharing_a_non_shareable_cast_creative_is_400(consent):
    async def go():
        h = Harness()
        if consent is not None:
            await _consent(h, **consent)
        await h.session(state=_cast_state("The Jackpot Reveal"))
        r = await h.create(concept_names=["The Jackpot Reveal"])
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "person_not_shareable"
        assert h.gcs.ops == [] and h.store.rows == {}

    run(go)


def test_slate_share_skips_cast_creatives_without_public_share_consent():
    async def go():
        h = Harness()
        await _consent(h, allow=False)
        await h.session(state=_cast_state("The Jackpot Reveal"))
        r = await h.create()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["skipped"] == [
            {"concept_name": "The Jackpot Reveal", "reason": "person_not_shareable"}
        ]
        assert "The Jackpot Reveal" not in body["concept_names"]
        row = await h.store.get(body["token"])
        assert row["person_consent_ids"] == []
        assert len(h.gcs.shared(body["token"])) == 4  # 3 images + snapshot

    run(go)


def test_slate_share_includes_cast_creatives_with_public_share_consent():
    async def go():
        h = Harness()
        await _consent(h, allow=True)
        await h.session(state=_cast_state("The Jackpot Reveal"))
        r = await h.create(include_eval=True)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["skipped"] == []
        token = body["token"]
        row = await h.store.get(token)
        assert row["person_consent_ids"] == [CONSENT]
        assert "person_consent_ids" not in body
        shared = h.gcs.shared(token)
        assert len(shared) == 5
        snapshot = shared[f"shares/{token}/snapshot.json"]["data"]
        assert CONSENT not in snapshot and "consent" not in snapshot
        assert len(json.loads(snapshot)["creatives"]) == 4
        # naming it works too
        r = await h.create(concept_names=["The Jackpot Reveal"])
        assert r.status_code == 200 and r.json()["scope"] == "creative"

    run(go)


def test_consent_lookup_failure_is_503_before_anything_is_written(monkeypatch):
    async def go():
        h = Harness()
        await h.session(state=_cast_state("The Jackpot Reveal"))

        async def boom(*_a):
            raise RuntimeError("bq down")

        monkeypatch.setattr(h.consents, "active_for", boom)
        r = await h.create()
        assert r.status_code == 503
        assert r.json()["detail"]["reason"] == "consent_unavailable"
        assert h.gcs.ops == [] and h.store.rows == {}
        # a run without cast creatives never looks consents up
        await h.session(sid="s2")
        assert (await h.create(sid="s2")).status_code == 200

    run(go)


def test_revoking_a_consent_revokes_its_shares():
    async def go():
        h = Harness()
        await _consent(h, allow=True)
        await h.session(state=_cast_state("The Jackpot Reveal"))
        cast_token = (await h.create()).json()["token"]
        other = (await h.create(concept_names=["The Authentic Encore"])).json()
        record = await h.consents.get(CONSENT)
        await sh.revoke_shares_for_consent(record)
        assert h.gcs.shared(cast_token) == {}
        assert (await h.store.get(cast_token))["revoked_at"] is not None
        assert len(h.gcs.shared(other["token"])) == 2  # untouched
        assert (await h.store.get(other["token"]))["revoked_at"] is None
        # idempotent repeat
        await sh.revoke_shares_for_consent(record)
        # another owner's record never touches A's shares
        await sh.revoke_shares_for_consent({**record, "owner_user": B})

    run(go)


def test_consent_share_cascade_failure_propagates_and_retry_finishes():
    async def go():
        h = Harness()
        await _consent(h, allow=True)
        await h.session(state=_cast_state("The Jackpot Reveal"))
        token = (await h.create()).json()["token"]
        record = await h.consents.get(CONSENT)
        real = h.gcs.bucket

        class Failing:
            def __init__(self, name):
                self.inner = real(name)

            def list_blobs(self, prefix=""):
                raise RuntimeError("gcs down")

        h.gcs.bucket = Failing
        with pytest.raises(RuntimeError):
            await sh.revoke_shares_for_consent(record)
        assert (await h.store.get(token))["revoked_at"] is not None
        h.gcs.bucket = real
        await sh.revoke_shares_for_consent(record)
        assert h.gcs.shared(token) == {}

    run(go)


JACKPOT = CONCEPTS.index("The Jackpot Reveal")


def test_consent_revoked_while_the_share_is_created_rolls_it_back():
    async def go():
        h = Harness()
        await _consent(h, allow=True)
        await h.session(state=_cast_state("The Jackpot Reveal"))
        real_put = h.store.put

        async def put_then_revoke(row):
            await real_put(row)
            await h.consents.revoke(CONSENT, A)  # the owner revokes meanwhile

        h.store.put = put_then_revoke
        r = await h.create()
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "person_consent_changed"
        (token,) = h.store.rows
        assert h.gcs.shared(token) == {}
        assert (await h.store.get(token))["revoked_at"] is not None

    run(go)


def test_share_without_cast_creatives_skips_the_recheck(monkeypatch):
    async def go():
        h = Harness()
        await h.session()
        calls = []

        async def spy(*a):
            calls.append(a)
            return None

        monkeypatch.setattr(pr, "active_consent", spy)
        assert (await h.create()).status_code == 200
        assert calls == []

    run(go)


def test_cast_render_seeded_as_uncast_is_refused_by_its_metadata():
    async def go():
        h = Harness()
        await _consent(h, allow=False)
        # state claims the creative is uncast, but the object says otherwise
        h.gcs.set_metadata(JACKPOT, {"consent_id": CONSENT})
        await h.session()
        r = await h.create()
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "person_not_shareable"
        assert not any(op[0] == "copy" for op in h.gcs.ops)
        assert h.store.rows == {}

    run(go)


def test_share_copies_carry_no_consent_metadata():
    async def go():
        h = Harness()
        await _consent(h, allow=True)
        h.gcs.set_metadata(JACKPOT, {"consent_id": CONSENT, "other": "x"})
        await h.session(state=_cast_state("The Jackpot Reveal"))
        r = await h.create()
        assert r.status_code == 200, r.text
        shared = h.gcs.shared(r.json()["token"])
        assert all(not o.get("metadata") for o in shared.values())
        # the source render keeps its metadata (the revoke cascade needs it)
        src = image_uri(JACKPOT).removeprefix(f"gs://{BUCKET}/")
        assert h.gcs.objects[BUCKET][src]["metadata"]["consent_id"] == CONSENT

    run(go)


@pytest.mark.parametrize(
    "folder",
    [
        {"gcs_folder": "other_run"},  # another run's renders
        {"gcs_folder": ""},
        {"gcs_folder": "shares"},
        {"agent_output_dir": ".."},
        {"agent_output_dir": "a/b"},
    ],
)
def test_images_only_from_this_runs_output_folder(folder):
    async def go():
        h = Harness()
        state = creative_state()
        state.update(folder)
        await h.session(state=state)
        r = await h.create()
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "image_outside_bucket"
        assert h.gcs.ops == [] and h.store.rows == {}

    run(go)


def test_image_outside_bucket_is_refused_before_any_copy():
    async def go():
        h = Harness()
        state = creative_state()
        state["generated_images"]["The Jackpot Reveal"]["gcs_uri"] = (
            "gs://someone-else/secret.png"
        )
        await h.session(state=state)
        r = await h.create()
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "image_outside_bucket"
        # a traversal inside the bucket is refused too
        state["generated_images"]["The Jackpot Reveal"]["gcs_uri"] = (
            f"gs://{BUCKET}/a/../shares/x/snapshot.json"
        )
        await h.session(sid="s2", state=state)
        r = await h.create(sid="s2")
        assert r.json()["detail"]["reason"] == "image_outside_bucket"
        assert h.gcs.ops == [] and h.store.rows == {}

    run(go)


@pytest.mark.parametrize("app", ["trend_scout", "bad-app", "creative_agent.x"])
def test_create_rejects_non_creative_apps(app):
    async def go():
        h = Harness()
        r = await h.create(app=app)
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "invalid_app_name"

    run(go)


def test_unknown_or_foreign_session_is_404():
    async def go():
        h = Harness()
        await h.session(user=B, sid="bob-s")
        assert (await h.create(sid="nope")).status_code == 404
        assert (await h.create(sid="bob-s")).status_code == 404
        await h.session(sid="s1")
        assert (await h.create(app="interactive_creative")).status_code == 404
        assert h.gcs.ops == []

    run(go)


def test_ownership_valueerror_maps_to_404():
    class OwnershipSvc:
        async def get_session(self, **_):
            raise ValueError(f"Session s1 does not belong to user {A}.")

    async def go():
        h = Harness()
        sh.configure(
            session_service=OwnershipSvc(),
            store=h.store,
            gcs_client=h.gcs,
            bucket=BUCKET,
        )
        assert (await h.create()).status_code == 404

    run(go)


@pytest.mark.parametrize("fail", [("copy", "/2.png"), ("upload", "snapshot.json")])
def test_partial_failure_rolls_back_and_is_502(fail):
    async def go():
        h = Harness()
        await h.session()
        h.gcs.fail = fail
        r = await h.create()
        assert r.status_code == 502
        assert r.json()["detail"]["reason"] == "share_failed"
        leftover = [n for n in h.gcs.objects[BUCKET] if n.startswith("shares/")]
        assert leftover == []
        assert any(op[0] == "delete" for op in h.gcs.ops)
        assert h.store.rows == {}

    run(go)


def test_store_failure_rolls_back_objects():
    class BrokenStore(InMemorySharesStore):
        async def put(self, row):
            raise RuntimeError("bq down")

    async def go():
        h = Harness()
        await h.session()
        sh.configure(
            session_service=h.svc,
            store=BrokenStore(),
            gcs_client=h.gcs,
            bucket=BUCKET,
        )
        r = await h.create()
        assert r.status_code == 502
        assert [n for n in h.gcs.objects[BUCKET] if n.startswith("shares/")] == []

    run(go)


def test_active_share_cap_is_429(monkeypatch):
    monkeypatch.setattr(sh, "MAX_ACTIVE_SHARES", 2)

    async def go():
        h = Harness()
        await h.session()
        assert (await h.create()).status_code == 200
        second = (await h.create()).json()["token"]
        r = await h.create()
        assert r.status_code == 429
        assert r.json()["detail"]["reason"] == "too_many_shares"
        # revoking frees a slot
        assert (await h.client.delete(f"/shares/{A}/{second}")).status_code == 204
        assert (await h.create()).status_code == 200

    run(go)


def test_unconfigured_bucket_is_503(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_STORAGE_BUCKET", raising=False)
    monkeypatch.delenv("GCS_BUCKET_NAME", raising=False)

    async def go():
        h = Harness()
        await h.session()
        sh.configure(session_service=h.svc, store=h.store, gcs_client=h.gcs)
        r = await h.create()
        assert r.status_code == 503
        assert r.json()["detail"]["reason"] == "shares_unconfigured"
        assert h.gcs.ops == []

    run(go)


# --- GET / DELETE -----------------------------------------------------------------


def test_list_shares_newest_first_and_owner_only():
    async def go():
        h = Harness()
        await h.session()
        await h.session(user=B, sid="bob-s")
        first = (await h.create()).json()["token"]
        second = (await h.create(concept_names=["The Authentic Encore"])).json()[
            "token"
        ]
        await h.create(user=B, sid="bob-s")
        r = await h.client.get(f"/shares/{A}")
        assert r.status_code == 200
        shares = r.json()["shares"]
        assert [s["token"] for s in shares] == [second, first]
        s = shares[0]
        assert set(s) == {
            "token",
            "url",
            "title",
            "scope",
            "include_eval",
            "concept_names",
            "app_name",
            "session_id",
            "created_at",
        }
        assert s["url"] == f"https://share.example/s/{second}"
        assert s["title"] == "For the Love of the Late-Night Jam."

    run(go)


def test_revoke_deletes_objects_and_hides_share():
    async def go():
        h = Harness()
        await h.session()
        token = (await h.create()).json()["token"]
        other = (await h.create()).json()["token"]
        r = await h.client.delete(f"/shares/{A}/{token}")
        assert r.status_code == 204
        assert h.gcs.shared(token) == {}
        assert len(h.gcs.shared(other)) == 5  # other shares untouched
        assert (await h.store.get(token))["revoked_at"] is not None
        listed = (await h.client.get(f"/shares/{A}")).json()["shares"]
        assert [s["token"] for s in listed] == [other]
        # the source images are never touched
        assert all(
            n in h.gcs.objects[BUCKET]
            for n in (image_uri(i).removeprefix(f"gs://{BUCKET}/") for i in range(4))
        )

    run(go)


def test_cross_owner_or_unknown_revoke_is_404():
    async def go():
        h = Harness()
        await h.session()
        token = (await h.create()).json()["token"]
        r = await h.client.delete(f"/shares/{B}/{token}")
        assert r.status_code == 404
        assert len(h.gcs.shared(token)) == 5
        r = await h.client.delete(f"/shares/{A}/{'x' * 22}")
        assert r.status_code == 404
        # malformed token
        r = await h.client.delete(f"/shares/{A}/short")
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "share_not_found"

    run(go)


# --- authz ------------------------------------------------------------------------


def test_enforce_mode_requires_trusted_matching_user():
    async def go():
        h = Harness(mode=AuthzMode.ENFORCE)
        await h.session()
        ok = {**PROXY, "X-TT-User": A}
        assert (await h.create()).status_code == 401
        assert (await h.create(headers={"X-TT-User": A})).status_code == 401
        assert (await h.create(user=B, headers=ok)).status_code == 403
        assert (await h.client.get(f"/shares/{A}")).status_code == 401
        assert (await h.client.get(f"/shares/{B}", headers=ok)).status_code == 403
        assert (
            await h.client.delete(f"/shares/{B}/{'x' * 22}", headers=ok)
        ).status_code == 403
        r = await h.create(headers=ok)
        assert r.status_code == 200
        token = r.json()["token"]
        assert (await h.client.get(f"/shares/{A}", headers=ok)).status_code == 200
        r = await h.client.delete(f"/shares/{A}/{token}", headers=ok)
        assert r.status_code == 204

    run(go)


# --- pure helpers -----------------------------------------------------------------


def test_person_images_are_never_shared():
    p = "run/out/"
    assert sh.source_path(f"gs://{BUCKET}/person-refs/me-1/me.jpg", BUCKET, p) is None
    assert (
        sh.source_path(f"gs://{BUCKET}/run/out/variants/me-1/c/k.png", BUCKET, p)
        is None
    )
    assert sh.source_path(f"gs://{BUCKET}/run/out/variants.png", BUCKET, p) == (
        "run/out/variants.png"
    )

    async def go():
        h = Harness()
        state = creative_state()
        state["generated_images"]["The Jackpot Reveal"]["gcs_uri"] = (
            f"gs://{BUCKET}/run/creative_output/variants/me-1/c/abc123abc123.png"
        )
        await h.session(state=state)
        r = await h.create()
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "person_image"
        state["generated_images"]["The Jackpot Reveal"]["gcs_uri"] = (
            f"gs://{BUCKET}/person-refs/me-1/me.jpg"
        )
        await h.session(sid="s2", state=state)
        r = await h.create(sid="s2")
        assert r.json()["detail"]["reason"] == "person_image"
        assert h.gcs.ops == [] and h.store.rows == {}

    run(go)


def test_source_path_only_inside_the_runs_output_folder():
    p = "a/out/"
    assert sh.source_path(f"gs://{BUCKET}/a/out/b.png", BUCKET, p) == "a/out/b.png"
    assert sh.source_path(f"gs://{BUCKET}/a/b.png", BUCKET, p) is None
    assert sh.source_path(f"gs://{BUCKET}/a/out/x/b.png", BUCKET, p) is None
    assert sh.source_path("gs://other/a/out/b.png", BUCKET, p) is None
    assert sh.source_path(f"gs://{BUCKET}/a/out/../b.png", BUCKET, p) is None
    assert sh.source_path(f"gs://{BUCKET}/shares/t/0.png", BUCKET, "shares/t/") is None
    assert sh.source_path("https://x/y.png", BUCKET, p) is None
    assert sh.source_path(f"gs://{BUCKET}/a/out/b.png", None, p) is None
    assert sh.source_path(f"gs://{BUCKET}/a/out/b.png", BUCKET, None) is None


def test_output_prefix_validates_segments():
    ok = {"gcs_folder": "2026_run", "agent_output_dir": "creative_output"}
    assert sh.output_prefix(ok) == "2026_run/creative_output/"
    for bad in (
        {"gcs_folder": "", "agent_output_dir": "o"},
        {"gcs_folder": "f"},
        {"gcs_folder": "f", "agent_output_dir": "a/b"},
        {"gcs_folder": "f", "agent_output_dir": ".."},
        {"gcs_folder": "person-refs", "agent_output_dir": "o"},
        {"gcs_folder": "shares", "agent_output_dir": "o"},
        {"gcs_folder": "f", "agent_output_dir": "variants"},
        {"gcs_folder": 3, "agent_output_dir": "o"},
    ):
        assert sh.output_prefix(bad) is None, bad


def test_share_url():
    assert sh.share_url("abc", "https://s.example/") == "https://s.example/s/abc"
    assert sh.share_url("abc", "") == "/s/abc"
