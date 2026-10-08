"""runserver/ratings.py: human creative ratings REST routes (offline).

In-memory session service + ``InMemoryRatingsStore``, driven over
``httpx.ASGITransport`` with the real ``UserAuthzMiddleware`` in front.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from google.adk.sessions import InMemorySessionService

from runserver import ratings as rt
from runserver.authz import AuthzMode, UserAuthzMiddleware, install_ownership_handler
from runserver.ratings_store import InMemoryRatingsStore, rating_id

A = "alice@example.com"
B = "bob@example.com"
APP = "creative_agent"
FIXTURES = Path(__file__).resolve().parents[1] / "frontend/scripts/screenshot-fixtures"
PROXY = {"Authorization": "Bearer proxy"}
VISUAL = "visual:The Golden Golf Cart Gig"
BUCKET = "trend-trawler-deploy-ae"  # the fixture's eval_report_gcs_uri bucket


def _report() -> dict:
    return json.loads((FIXTURES / "creative-eval-report.json").read_text())


def _state(with_report: bool = True) -> dict:
    state = json.loads((FIXTURES / "creative-state.json").read_text())
    if with_report:
        state["creative_evaluation_report"] = _report()
    return state


class Harness:
    def __init__(self, mode=AuthzMode.TRUST_CLIENT, report_loader=None):
        self.svc = InMemorySessionService()
        self.store = InMemoryRatingsStore()
        self.loads: list[str] = []

        def default_loader(uri: str):
            self.loads.append(uri)
            return _report()

        rt.configure(
            session_service=self.svc,
            store=self.store,
            report_loader=report_loader or default_loader,
            report_bucket=BUCKET,
        )
        app = FastAPI()
        app.include_router(rt.router)
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
            state=_state() if state is None else state,
        )

    async def put(self, user=A, sid="s1", headers=None, **over):
        body = {
            "app_name": APP,
            "creative_key": VISUAL,
            "kind": "visual",
            "verdict": "pass",
            **over,
        }
        return await self.client.put(
            f"/ratings/{user}/{sid}", json=body, headers=headers or {}
        )


def run(coro_fn):
    return asyncio.run(coro_fn())


# --- pure helpers -----------------------------------------------------------------


def test_creative_index_from_fixture():
    idx = rt.creative_index(_state())
    assert set(idx) == {
        "visual:The Golden Golf Cart Gig",
        "visual:Nihilistic Retirement Plan",
        "visual:The Jackpot Reveal",
        "visual:The Authentic Encore",
        "copy:1",
        "copy:3",
        "copy:5",
        "copy:8",
    }
    assert idx["copy:3"]["kind"] == "ad_copy"
    # JSON-string state values and float ids are accepted too
    state = _state()
    state["final_visual_concepts"] = json.dumps(state["final_visual_concepts"])
    state["ad_copy_critique"]["ad_copies"][0]["original_id"] = 1.0
    assert "copy:1" in rt.creative_index(state)
    assert "visual:The Jackpot Reveal" in rt.creative_index(state)
    assert rt.creative_index({}) == {}


def test_judge_fields_visual_and_copy():
    report = _report()
    idx = rt.creative_index(_state())
    vis = rt.judge_fields(report, idx[VISUAL])
    assert vis["judge_overall"] == pytest.approx(0.867)
    assert vis["judge_passed"] is True
    assert vis["judge_gates_passed"] is None  # pre-gates report
    # every ad-copy eval echoes original_id 1: matched by headline, not id
    want = next(
        e
        for e in report["ad_copy_evaluations"]
        if e["headline"] == idx["copy:8"]["headline"]
    )
    copy8 = rt.judge_fields(report, idx["copy:8"])
    assert copy8["judge_overall"] == want["score"]["overall_score"]


def test_judge_fields_optional_gates_and_model():
    report = _report()
    report["judge_model"] = "gemini-3.1-pro-preview"
    score = report["visual_concept_evaluations"][0]["score"]
    score["gates"] = [{"gate": "product_visible", "passed": False, "note": "no guitar"}]
    score["gates_passed"] = False
    out = rt.judge_fields(report, rt.creative_index(_state())[VISUAL])
    assert out["judge_gates_passed"] is False
    assert out["judge_model"] == "gemini-3.1-pro-preview"


def test_judge_gates_passed_ignored_without_recorded_gates():
    """gates_passed defaults to True in CreativeScore: a gate-less report must not
    read as "every gate passed" (false agreement in the calibration)."""
    report = _report()
    score = report["visual_concept_evaluations"][0]["score"]
    score["gates"], score["gates_passed"] = [], True
    out = rt.judge_fields(report, rt.creative_index(_state())[VISUAL])
    assert out["judge_gates_passed"] is None
    score["gates_passed"] = "yes"  # malformed
    score["gates"] = [{"gate": "g", "passed": True}]
    assert (
        rt.judge_fields(report, rt.creative_index(_state())[VISUAL])[
            "judge_gates_passed"
        ]
        is None
    )


def test_judge_fields_ambiguous_or_missing_is_none():
    """Conservative matching: no guess when the judge's ids are ambiguous."""
    report = _report()
    info = {"kind": "ad_copy", "original_id": "1", "headline": "not in report"}
    # headline misses; original_id 1 is echoed by all four evals -> ambiguous
    assert rt.judge_fields(report, info)["judge_overall"] is None
    info_unique = {"kind": "ad_copy", "original_id": "1", "headline": None}
    for i, ev in enumerate(report["ad_copy_evaluations"]):
        ev["original_id"] = i + 1
    assert rt.judge_fields(report, info_unique)["judge_overall"] is not None
    assert rt.judge_fields(None, info)["judge_passed"] is None
    assert rt.judge_fields("not json", info)["judge_model"] is None
    dup = _report()
    dup["visual_concept_evaluations"].append(dup["visual_concept_evaluations"][0])
    assert (
        rt.judge_fields(dup, {"kind": "visual", "name": "The Golden Golf Cart Gig"})[
            "judge_overall"
        ]
        is None
    )


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"app_name": "trend_scout"}, "invalid_app_name"),
        ({"kind": "image"}, "invalid_kind"),
        ({"kind": "ad_copy"}, "invalid_creative_key"),  # visual: key, ad_copy kind
        ({"creative_key": "visual:"}, "invalid_creative_key"),
        ({"creative_key": 3}, "invalid_creative_key"),
        ({"verdict": "maybe"}, "invalid_verdict"),
        ({"verdict": None}, "invalid_verdict"),
        ({"score": 0}, "invalid_score"),
        ({"score": 6}, "invalid_score"),
        ({"score": 3.5}, "invalid_score"),
        ({"score": True}, "invalid_score"),
        ({"score": "4"}, "invalid_score"),
        ({"note": 5}, "invalid_note"),
        ({"note": "x" * 2001}, "invalid_note"),
    ],
)
def test_validate_rating_rejects(over, reason):
    body = {
        "app_name": APP,
        "creative_key": VISUAL,
        "kind": "visual",
        "verdict": "pass",
        **over,
    }
    with pytest.raises(rt.RatingError) as e:
        rt.validate_rating(body)
    assert e.value.reason == reason


def test_validate_rating_accepts_and_normalises_note():
    out = rt.validate_rating(
        {
            "app_name": "interactive_creative",
            "creative_key": "copy:3",
            "kind": "ad_copy",
            "verdict": "fail",
            "score": 5,
            "note": "   ",
        }
    )
    assert out["note"] is None and out["score"] == 5


# --- routes -----------------------------------------------------------------------


def test_put_then_get_and_upsert_is_idempotent():
    async def go():
        h = Harness()
        await h.session()
        r1 = await h.put(score=4, note="Strong hook")
        assert r1.status_code == 200, r1.text
        body = r1.json()
        assert body["rating_id"] == rating_id("s1", VISUAL, A)
        assert body["judge_overall"] == pytest.approx(0.867)
        assert body["judge_passed"] is True
        assert "user_id" not in body
        first = dict(h.store.rows[body["rating_id"]])
        r2 = await h.put(verdict="fail", score=None, note=None)
        assert r2.status_code == 200
        assert len(h.store.rows) == 1  # same row, updated
        row = h.store.rows[body["rating_id"]]
        assert row["verdict"] == "fail" and row["score"] is None
        assert row["created_at"] == first["created_at"]
        assert row["updated_at"] >= first["updated_at"]
        await h.put(creative_key="copy:3", kind="ad_copy")
        listed = (await h.client.get(f"/ratings/{A}/s1")).json()["ratings"]
        assert [r["creative_key"] for r in listed] == ["copy:3", VISUAL]
        assert listed[1]["created_at"] and listed[1]["verdict"] == "fail"
        assert (await h.client.get(f"/ratings/{A}/other")).json() == {"ratings": []}

    run(go)


def test_put_unknown_creative_key_is_400():
    async def go():
        h = Harness()
        await h.session()
        r = await h.put(creative_key="visual:Not A Concept")
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "unknown_creative_key"
        r = await h.put(creative_key="copy:99", kind="ad_copy")
        assert r.json()["detail"]["reason"] == "unknown_creative_key"
        assert h.store.rows == {}

    run(go)


def test_put_bad_body_is_400_not_422():
    async def go():
        h = Harness()
        await h.session()
        r = await h.put(score="five")
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "invalid_score"

    run(go)


def test_put_unknown_or_foreign_session_is_404():
    async def go():
        h = Harness()
        await h.session(user=B, sid="bob-s")
        assert (await h.put(sid="nope")).status_code == 404
        # Alice naming Bob's session id under her own user: not found for her.
        assert (await h.put(sid="bob-s")).status_code == 404
        # Wrong app for an existing session id
        await h.session(sid="s1")
        assert (await h.put(app_name="interactive_creative")).status_code == 404

    run(go)


def test_ownership_valueerror_maps_to_404():
    class OwnershipSvc:
        async def get_session(self, **_):
            raise ValueError(f"Session s1 does not belong to user {A}.")

    async def go():
        h = Harness()
        rt.configure(session_service=OwnershipSvc(), store=h.store)
        assert (await h.put()).status_code == 404

    run(go)


def test_report_falls_back_to_gcs_and_is_cached():
    async def go():
        h = Harness()
        state = _state(with_report=False)
        await h.session(state=state)
        r = await h.put()
        assert r.json()["judge_overall"] == pytest.approx(0.867)
        await h.put(creative_key="copy:3", kind="ad_copy")
        assert h.loads == [state["eval_report_gcs_uri"]]  # one GCS read

    run(go)


def test_report_load_failure_fails_soft():
    def boom(uri):
        raise OSError("gcs down")

    async def go():
        h = Harness(report_loader=boom)
        await h.session(state=_state(with_report=False))
        r = await h.put()
        assert r.status_code == 200
        assert r.json()["judge_overall"] is None and r.json()["judge_passed"] is None

    run(go)


def test_store_failure_is_502():
    class Broken(InMemoryRatingsStore):
        async def upsert(self, row):
            raise RuntimeError("bq down")

    async def go():
        h = Harness()
        await h.session()
        rt.configure(session_service=h.svc, store=Broken())
        r = await h.put()
        assert r.status_code == 502
        assert r.json()["detail"]["reason"] == "store_failed"

    run(go)


def test_enforce_mode_authz():
    async def go():
        h = Harness(mode=AuthzMode.ENFORCE)
        await h.session()
        ok = {**PROXY, "X-TT-User": A}
        assert (await h.put(headers=ok)).status_code == 200
        # no trusted identity
        assert (await h.put()).status_code == 401
        # header without the proxy's token is not trusted
        assert (await h.put(headers={"X-TT-User": A})).status_code == 401
        # path user != trusted user
        assert (await h.put(user=B, headers=ok)).status_code == 403
        assert (await h.client.get(f"/ratings/{B}/s1", headers=ok)).status_code == 403
        assert (await h.client.get(f"/ratings/{A}/s1")).status_code == 401
        r = await h.client.get(f"/ratings/{A}/s1", headers=ok)
        assert r.status_code == 200 and len(r.json()["ratings"]) == 1
        # Bob (trusted as Bob) sees none of Alice's ratings
        bob = {**PROXY, "X-TT-User": B}
        r = await h.client.get(f"/ratings/{B}/s1", headers=bob)
        assert r.json() == {"ratings": []}

    run(go)


def test_calibration_endpoint_aggregates_the_users_ratings():
    async def go():
        h = Harness()
        await h.session()
        empty = (await h.client.get(f"/ratings/{A}/calibration")).json()
        assert empty["n"] == 0
        assert empty["overall"]["judge_passed"]["reason"] == "no_pairs"
        # fixture judge: Golden Golf Cart passes; copy:3 is matched by headline
        await h.put(verdict="pass", score=5)
        await h.put(creative_key="visual:The Jackpot Reveal", verdict="fail", score=2)
        await h.put(creative_key="copy:3", kind="ad_copy", verdict="pass")
        await h.session(user=B, sid="s2")
        await h.put(user=B, sid="s2", verdict="fail")  # Bob's: not counted
        rep = (await h.client.get(f"/ratings/{A}/calibration")).json()
        assert rep["n"] == 3 and rep["sessions"] == 1
        assert rep["by_kind"]["visual"]["n"] == 2
        assert rep["by_kind"]["ad_copy"]["judge_passed"]["n"] == 1
        assert rep["overall"]["judge_passed"]["agreement"] is not None
        # "calibration" is not mistaken for a session id
        assert "ratings" not in rep

    run(go)


def test_calibration_requires_matching_user_in_enforce_mode():
    async def go():
        h = Harness(mode=AuthzMode.ENFORCE)
        ok = {**PROXY, "X-TT-User": A}
        assert (await h.client.get(f"/ratings/{A}/calibration")).status_code == 401
        r = await h.client.get(f"/ratings/{B}/calibration", headers=ok)
        assert r.status_code == 403
        r = await h.client.get(f"/ratings/{A}/calibration", headers=ok)
        assert r.status_code == 200

    run(go)


def test_store_read_failure_is_502():
    class Broken(InMemoryRatingsStore):
        async def list_for_user(self, user_id):
            raise RuntimeError("bq down")

        async def list_for_session(self, user_id, session_id):
            raise RuntimeError("bq down")

    async def go():
        h = Harness()
        rt.configure(session_service=h.svc, store=Broken())
        assert (await h.client.get(f"/ratings/{A}/calibration")).status_code == 502
        assert (await h.client.get(f"/ratings/{A}/s1")).status_code == 502

    run(go)


def test_judge_fields_read_a_real_gated_report_model():
    """Field locations pinned against creative_eval's own report models."""
    from creative_eval.schemas import (
        CreativeScore,
        GateResult,
        VisualConceptEvaluation,
    )

    ev = VisualConceptEvaluation(
        ad_copy_id=1,
        concept_name="The Golden Golf Cart Gig",
        score=CreativeScore(
            overall_score=0.8,
            passed=False,
            verdicts=[],
            strengths=[],
            improvements=[],
            gates=[GateResult(gate="product_visible", passed=False, note="no guitar")],
            gates_passed=False,
        ),
    )
    report = {"visual_concept_evaluations": [ev.model_dump()], "judge_model": "j"}
    out = rt.judge_fields(report, rt.creative_index(_state())[VISUAL])
    assert out == {
        "judge_overall": 0.8,
        "judge_passed": False,
        "judge_gates_passed": False,
        "judge_model": "j",
    }


# --- report source hardening -------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "gs://other-bucket/2026/creative_output/creative_eval_report.json",
        "https://storage.googleapis.com/trend-trawler-deploy-ae/x/creative_eval_report.json",
        "gs://trend-trawler-deploy-ae/x/creative_output/secrets.json",
        "gs://trend-trawler-deploy-ae/creative_eval_report.json.bak",
        "gs://trend-trawler-deploy-ae/a/../creative_eval_report.json",
        "gs://trend-trawler-deploy-ae-evil/x/creative_eval_report.json",
    ],
)
def test_untrusted_report_uri_is_never_read(uri):
    async def go():
        h = Harness()
        state = _state(with_report=False)
        state["eval_report_gcs_uri"] = uri
        await h.session(state=state)
        r = await h.put()
        assert r.status_code == 200  # rating still saved
        body = r.json()
        assert body["judge_overall"] is None and body["judge_passed"] is None
        assert body["judge_source"] == "none"
        assert h.loads == []

    run(go)


def test_allowed_report_uri():
    ok = "gs://b/run/creative_output/creative_eval_report.json"
    assert rt.allowed_report_uri(ok, "b")
    assert not rt.allowed_report_uri(ok, None)  # no configured bucket: never read
    assert not rt.allowed_report_uri(ok, "c")
    assert not rt.allowed_report_uri(None, "b")


def test_configured_report_bucket_env():
    assert (
        rt.configured_report_bucket({"GOOGLE_CLOUD_STORAGE_BUCKET": "gs://a/"}) == "a"
    )
    assert rt.configured_report_bucket({"GCS_BUCKET_NAME": "b"}) == "b"
    both = {"GOOGLE_CLOUD_STORAGE_BUCKET": "a", "GCS_BUCKET_NAME": "b"}
    assert rt.configured_report_bucket(both) == "a"
    assert rt.configured_report_bucket({}) is None


class _Blob:
    def __init__(self, size, text="{}"):
        self.size, self.text, self.reloaded, self.downloaded = size, text, False, False

    def reload(self):
        self.reloaded = True

    def download_as_text(self):
        self.downloaded = True
        return self.text


def _patch_gcs(monkeypatch, blob):
    class Bucket:
        def blob(self, path):
            return blob

    class Client:
        def bucket(self, name):
            return Bucket()

    monkeypatch.setattr("agent_common.clients.get_gcs_client", lambda: Client())


def test_gcs_loader_rejects_oversize_before_download(monkeypatch):
    blob = _Blob(rt.REPORT_MAX_BYTES + 1)
    _patch_gcs(monkeypatch, blob)
    with pytest.raises(ValueError, match="bytes"):
        rt.gcs_report_loader("gs://b/x/creative_eval_report.json")
    assert blob.reloaded and not blob.downloaded
    small = _Blob(10, '{"judge_model": "m"}')
    _patch_gcs(monkeypatch, small)
    assert rt.gcs_report_loader("gs://b/x/creative_eval_report.json") == {
        "judge_model": "m"
    }


def test_oversize_report_fails_soft_through_the_route(monkeypatch):
    _patch_gcs(monkeypatch, _Blob(rt.REPORT_MAX_BYTES + 1))

    async def go():
        h = Harness()
        rt.configure(session_service=h.svc, store=h.store, report_bucket=BUCKET)
        await h.session(state=_state(with_report=False))
        r = await h.put()
        assert r.status_code == 200
        assert r.json()["judge_overall"] is None
        assert r.json()["judge_source"] == "none"

    run(go)


def test_judge_source_prefers_the_gcs_report_over_state():
    async def go():
        h = Harness()
        await h.session()  # state has both the report and a trusted uri
        assert (await h.put()).json()["judge_source"] == "gcs"
        state = _state()
        del state["eval_report_gcs_uri"]
        await h.session(sid="s2", state=state)
        assert (await h.put(sid="s2")).json()["judge_source"] == "state"
        state["eval_report_gcs_uri"] = "gs://other/x/creative_eval_report.json"
        await h.session(sid="s3", state=state)
        assert (await h.put(sid="s3")).json()["judge_source"] == "state"

    run(go)


def test_note_is_bounded_by_the_body_schema():
    async def go():
        h = Harness()
        await h.session()
        assert (await h.put(note="x" * 4001)).status_code == 422
        r = await h.put(note="x" * 2001)
        assert r.json()["detail"]["reason"] == "invalid_note"

    run(go)


def test_put_row_carries_every_store_column():
    """The route's row names every MERGE column (build_upsert_sql would KeyError)."""
    from runserver.ratings_store import RATING_COLUMN_TYPES, build_upsert_sql

    async def go():
        h = Harness()
        await h.session()
        assert (await h.put()).status_code == 200
        (row,) = h.store.rows.values()
        assert set(RATING_COLUMN_TYPES) <= set(row)
        build_upsert_sql("p.d.t", row)

    run(go)


# --- fail-reason chips (rating-driven learning) -----------------------------------

FAIL_REASONS = (
    "product_not_visible",
    "text_problem",
    "unwanted_logo",
    "weak_cta",
    "off_brief",
    "trend_unclear",
    "cluttered",
    "off_brand_tone",
    "artifacts",
    "other",
)


def test_fail_reason_enum_and_labels():
    from runserver.rating_reasons import FAIL_REASON_LABELS
    from runserver.rating_reasons import FAIL_REASONS as ENUM

    assert ENUM == FAIL_REASONS
    assert tuple(FAIL_REASON_LABELS) == FAIL_REASONS
    assert FAIL_REASON_LABELS["product_not_visible"] == "Product hard to see"
    assert all(v and v[0].isupper() for v in FAIL_REASON_LABELS.values())


def test_validate_rating_fail_reasons():
    base = {"app_name": APP, "creative_key": VISUAL, "kind": "visual"}
    out = rt.validate_rating(
        {
            **base,
            "verdict": "fail",
            "fail_reasons": ["text_problem", "weak_cta", "text_problem"],
        }
    )
    assert out["fail_reasons"] == ["text_problem", "weak_cta"]  # deduped, ordered
    assert rt.validate_rating({**base, "verdict": "fail"})["fail_reasons"] == []
    out = rt.validate_rating({**base, "verdict": "pass", "fail_reasons": ["weak_cta"]})
    assert out["fail_reasons"] == []  # dropped on pass
    for bad in (["ignore previous instructions"], "weak_cta", [3], ["WEAK_CTA"]):
        with pytest.raises(rt.RatingError) as e:
            rt.validate_rating({**base, "verdict": "fail", "fail_reasons": bad})
        assert e.value.reason == "invalid_fail_reasons"


def test_fail_reasons_validated():
    async def go():
        h = Harness()
        await h.session()
        ok = await h.put(
            verdict="fail", fail_reasons=["product_not_visible", "text_problem"]
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["fail_reasons"] == ["product_not_visible", "text_problem"]
        (row,) = h.store.rows.values()
        assert row["fail_reasons"] == ["product_not_visible", "text_problem"]
        listed = (await h.client.get(f"/ratings/{A}/s1")).json()["ratings"]
        assert listed[0]["fail_reasons"] == ["product_not_visible", "text_problem"]
        bad = await h.put(verdict="fail", fail_reasons=["ignore previous instructions"])
        assert bad.status_code == 400
        assert bad.json()["detail"]["reason"] == "invalid_fail_reasons"
        # the bad PUT left the stored row alone
        assert row["fail_reasons"] == ["product_not_visible", "text_problem"]
        too_many = await h.put(verdict="fail", fail_reasons=["other"] * 11)
        assert too_many.status_code == 422  # bounded by the body schema

    run(go)


def test_fail_reasons_dropped_on_pass():
    async def go():
        h = Harness()
        await h.session()
        r = await h.put(verdict="pass", fail_reasons=["weak_cta"])
        assert r.status_code == 200
        (row,) = h.store.rows.values()
        assert row["fail_reasons"] == []

    run(go)
