"""runserver/experiments.py: REST routes, authz, state machine, reaper (offline).

Drives the router over ``httpx.ASGITransport`` inside one ``asyncio.run`` per test,
so detached deploy/teardown tasks share the loop and ``experiments.drain()`` can
await them deterministically. Backends are the in-memory/fake implementations.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from google.adk.sessions import InMemorySessionService

from agent_common.idempotency import stable_row_id
from runserver import experiments as ex
from runserver.authz import AuthzMode, UserAuthzMiddleware, install_ownership_handler
from runserver.experiments_deploy import FakeDeployer
from runserver.experiments_jobs import FakeJobsRunner
from runserver.experiments_store import InMemoryExperimentStore

A = "alice@example.com"
B = "bob@example.com"
APP = "creative_agent"
FIXTURES = Path(__file__).resolve().parents[1] / "frontend/scripts/screenshot-fixtures"


def _fixture_state() -> dict:
    state = json.loads((FIXTURES / "creative-state.json").read_text())
    state["creative_evaluation_report"] = json.loads(
        (FIXTURES / "creative-eval-report.json").read_text()
    )
    return state


class Harness:
    def __init__(self, mode=AuthzMode.TRUST_CLIENT, deployer=None, wait=2.0):
        self.svc = InMemorySessionService()
        self.store = InMemoryExperimentStore()
        self.deployer = deployer or FakeDeployer()
        self.jobs = FakeJobsRunner()
        self.writer = ex.MemoryConfigWriter()
        self.settings = ex.ExperimentSettings(
            artifacts_prefix="gs://bkt/bandit",
            config_writer=self.writer,
            gcs_bucket="bkt",
            teardown_wait_seconds=wait,
        )
        ex.configure(
            session_service=self.svc,
            store=self.store,
            deployer=self.deployer,
            jobs=self.jobs,
            authz_mode=mode,
            settings=self.settings,
        )
        app = FastAPI()
        app.include_router(ex.router)
        install_ownership_handler(app)
        app.add_middleware(
            UserAuthzMiddleware, mode=mode, caller_ok=lambda a: a == "Bearer proxy"
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    async def session(self, user=A, sid="s1", state=None):
        await self.svc.create_session(
            app_name=APP,
            user_id=user,
            session_id=sid,
            state=_fixture_state() if state is None else state,
        )

    async def create(self, user=A, sid="s1", indices=(0, 1), headers=None, **over):
        body = {
            "userId": user,
            "appName": APP,
            "sessionId": sid,
            "creativeIndices": list(indices),
            "scenario": "clear_winner",
            "ctrMode": "demo",
            "rewardMode": "click",
            **over,
        }
        return await self.client.post("/experiments", json=body, headers=headers or {})


def run(coro_fn):
    async def go():
        try:
            return await coro_fn()
        finally:
            # Bounded: a failed assertion can leave a gated fake deploy waiting.
            try:
                await asyncio.wait_for(ex.drain(), 10)
            except TimeoutError:
                for task in list(ex._BACKGROUND_TASKS):
                    task.cancel()

    return asyncio.run(go())


# --- pure helpers -----------------------------------------------------------------


def test_snapshot_arms_from_fixture():
    state = _fixture_state()
    arms = ex.snapshot_arms(state, "sess-1", [0, 3])
    assert [a["index"] for a in arms] == [0, 3]
    a0, a3 = arms
    assert a0["creativeId"] == stable_row_id("sess-1", "The Golden Golf Cart Gig")
    assert a0["conceptName"] == "The Golden Golf Cart Gig"
    assert a0["label"] == "If I Won The $1.8B Powerball, There To Be Signs."
    assert a0["imageUri"] == (
        "gs://trend-trawler-deploy-ae/2026_07_16_23_51_47c9/creative_output/"
        "The_Golden_Golf_Cart_Gig.png"
    )
    assert a0["scores"]["visual_overall"] == pytest.approx(0.867)
    assert a0["scores"]["ad_copy_overall"] == pytest.approx(0.85)
    assert a0["scores"]["copy_quality"] == pytest.approx(0.6)
    assert a0["scores"]["trend_visual_connection"] == pytest.approx(0.9)
    assert len([k for k in a0["scores"] if not k.endswith("_overall")]) == 12
    assert a0["overallScore"] == pytest.approx((0.867 + 0.85) / 2)
    # every ad-copy eval echoes original_id 1: matched by headline, not id
    report = state["creative_evaluation_report"]
    want = next(
        e
        for e in report["ad_copy_evaluations"]
        if e["headline"] == "For the Love of the Late-Night Jam."
    )
    assert a3["scores"]["ad_copy_overall"] == want["score"]["overall_score"]
    assert all(0.0 <= v <= 1.0 for v in a3["scores"].values())


def test_snapshot_arms_without_evals_or_images():
    state = _fixture_state()
    del state["creative_evaluation_report"]
    state["_generated_artifact_keys"] = ["The_Jackpot_Reveal.png"]
    for k in ("gcs_bucket_name", "gcs_bucket"):
        del state[k]
    arms = ex.snapshot_arms(state, "s", [1, 2], default_bucket="fallback")
    assert arms[0]["imageUri"] is None  # never rendered
    assert arms[1]["imageUri"].startswith("gs://fallback/")
    assert arms[0]["scores"] == {} and arms[0]["overallScore"] is None
    # JSON-string state values are accepted too
    state["final_visual_concepts"] = json.dumps(state["final_visual_concepts"])
    assert len(ex.snapshot_arms(state, "s", [0, 1])) == 2


@pytest.mark.parametrize(
    ("indices", "reason"),
    [
        ([], "too_few_arms"),
        ([0], "too_few_arms"),
        ([0, 1, 2, 3, 0], "too_many_arms"),
        ([0, 0], "duplicate_index"),
        ([0, 9], "index_out_of_range"),
        ([-1, 0], "index_out_of_range"),
    ],
)
def test_snapshot_arms_rejects_bad_selection(indices, reason):
    with pytest.raises(ex.SelectionError) as e:
        ex.snapshot_arms(_fixture_state(), "s", indices)
    assert e.value.reason == reason


def test_build_experiment_config_matches_section_1_shape():
    state = _fixture_state()
    arms = ex.snapshot_arms(state, "s", [0, 1])
    cfg = ex.build_experiment_config(
        "e1",
        arms,
        "drift",
        "realistic",
        "engaged",
        styles=ex.visual_styles(state, arms),
    )
    assert set(cfg) == {
        "experiment_id", "arms", "scenario", "ctr_mode", "reward_mode", "horizon",
        "batch_size", "episodes", "seed", "policy",
    }  # fmt: skip
    assert cfg["arms"][0] == {
        "creative_id": arms[0]["creativeId"],
        "label": arms[0]["label"],
        "scores": arms[0]["scores"],
        "visual_style": "Surreal Meme-Collage",
    }
    assert cfg["policy"] == {
        "prior_var": 1.0,
        "noise_var": 0.25,
        "exploration_scale": 1.0,
        "propensity_samples": 1000,
        "min_propensity": 0.02,
        "discount": 1.0,
    }
    assert (cfg["horizon"], cfg["batch_size"], cfg["episodes"]) == (20000, 100, 20)
    assert 0 <= cfg["seed"] < 2**31
    assert ex.build_experiment_config("e1", arms, "drift")["seed"] == cfg["seed"]


def test_next_status_machine():
    path = [
        ("deploying", "deployed", "ready"),
        ("ready", "traffic_started", "running_traffic"),
        ("running_traffic", "traffic_done", "ready"),
        ("ready", "stop", "stopping"),
        ("stopping", "torn_down", "stopped"),
    ]
    for cur, event, want in path:
        assert ex.next_status(cur, event) == want
    for cur in ("deploying", "ready", "running_traffic"):
        assert ex.next_status(cur, "fail") == "failed"
        assert ex.next_status(cur, "expire") == "expired"
    for cur, event in [
        ("stopped", "deployed"),
        ("failed", "stop"),
        ("expired", "expire"),
        ("deploying", "traffic_started"),
        ("stopping", "fail"),
    ]:
        with pytest.raises(ex.InvalidTransition):
            ex.next_status(cur, event)


def test_is_expired_and_to_summary():
    now = dt.datetime(2026, 10, 2, tzinfo=dt.UTC)
    row = {
        "experiment_id": "e",
        "user_id": A,
        "session_id": "s",
        "app_name": APP,
        "created_at": now,
        "updated_at": now,
        "status": "ready",
        "ttl_expires_at": now,
        "arms": [],
        "progress": {"episodes_done": 2, "episodes_total": 5},
    }
    assert ex.is_expired(row, now)
    assert not ex.is_expired(row, now - dt.timedelta(seconds=1))
    assert not ex.is_expired({**row, "status": "stopped"}, now)
    assert not ex.is_expired({**row, "ttl_expires_at": None}, now)
    s = ex.to_summary(row)
    assert s["createdAt"] == "2026-10-02T00:00:00Z"
    assert s["progress"] == {"episodesDone": 2, "episodesTotal": 5}
    assert s["endpointId"] is None and s["error"] is None
    assert set(s) == {
        "experimentId", "userId", "sessionId", "appName", "createdAt", "updatedAt",
        "status", "scenario", "ctrMode", "rewardMode", "ttlExpiresAt", "arms",
        "endpointId", "trafficExecution", "progress", "error",
    }  # fmt: skip


def test_build_backend_from_env():
    fake = ex.build_backend_from_env({"BANDIT_DEPLOY_MODE": "fake"})
    assert fake["mode"] == "fake"
    assert isinstance(fake["store"], InMemoryExperimentStore)
    # vertex without BigQuery env falls back to fake (local dev)
    assert ex.build_backend_from_env({})["mode"] == "fake"
    full = ex.build_backend_from_env(
        {
            "BQ_PROJECT_ID": "p",
            "BQ_DATASET_ID": "d",
            "GOOGLE_CLOUD_STORAGE_BUCKET": "bkt",
            "BANDIT_SERVING_IMAGE": "img",
            "BANDIT_TTL_MINUTES": "30",
        }
    )
    assert full["mode"] == "vertex"
    assert full["settings"].artifacts_prefix == "gs://bkt/bandit"
    assert full["settings"].ttl_minutes == 30
    assert full["jobs"].job_name.endswith("/jobs/trend-trawler-bandit-traffic")
    with pytest.raises(RuntimeError):
        ex.build_backend_from_env({"BANDIT_DEPLOY_MODE": "bogus"})


# --- routes -----------------------------------------------------------------------


def test_create_deploys_to_ready_and_writes_config():
    h = Harness()

    async def go():
        await h.session()
        r = await h.create(indices=[2, 0], ttlMinutes=30)
        assert r.status_code == 201, r.text
        eid = r.json()["experimentId"]
        assert r.json() == {"experimentId": eid, "status": "deploying"}
        await ex.drain()
        detail = (await h.client.get(f"/experiments/{A}/{eid}")).json()
        listed = (await h.client.get(f"/experiments/{A}")).json()
        return eid, detail, listed

    eid, detail, listed = run(go)
    assert detail["status"] == "ready"
    assert detail["endpointId"].startswith("projects/fake/")
    assert [a["index"] for a in detail["arms"]] == [2, 0]
    created = dt.datetime.fromisoformat(detail["createdAt"])
    ttl = dt.datetime.fromisoformat(detail["ttlExpiresAt"])
    assert ttl - created == dt.timedelta(minutes=30)
    uri = f"gs://bkt/bandit/{eid}/experiment.json"
    assert h.store.rows[eid]["config_uri"] == uri
    cfg = h.writer.files[uri]
    assert cfg["experiment_id"] == eid and len(cfg["arms"]) == 2
    assert [e["experimentId"] for e in listed["experiments"]] == [eid]
    assert h.deployer.created == {
        "model_resource": 1,
        "endpoint_id": 1,
        "deployed_model_id": 1,
    }


def test_create_validation_errors():
    h = Harness()

    async def go():
        await h.session()
        empty = await h.create(indices=[])
        one = await h.create(indices=[1])
        oob = await h.create(indices=[0, 7])
        bad_scenario = await h.create(scenario="nope")
        missing = await h.create(sid="nope")
        return empty, one, oob, bad_scenario, missing

    empty, one, oob, bad_scenario, missing = run(go)
    assert (
        empty.status_code == 400 and empty.json()["detail"]["reason"] == "too_few_arms"
    )
    assert one.status_code == 400
    assert oob.json()["detail"]["reason"] == "index_out_of_range"
    assert bad_scenario.json()["detail"]["reason"] == "invalid_scenario"
    assert missing.status_code == 404
    assert not h.store.rows


def test_second_active_experiment_is_409():
    h = Harness()

    async def go():
        await h.session()
        first = await h.create()
        second = await h.create()
        await ex.drain()
        await h.client.post(f"/experiments/{A}/{first.json()['experimentId']}/stop")
        await ex.drain()
        third = await h.create()
        return first, second, third

    first, second, third = run(go)
    assert second.status_code == 409
    assert second.json()["detail"]["reason"] == "active_experiment"
    assert second.json()["detail"]["experimentId"] == first.json()["experimentId"]
    assert third.status_code == 201


def test_traffic_refused_unless_ready_then_starts_job():
    h = Harness()
    h.deployer.gate = asyncio.Event()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        url = f"/experiments/{A}/{eid}/traffic"
        early = await h.client.post(url, json={"episodes": 5})
        h.deployer.gate.set()
        await ex.drain()
        bad = await h.client.post(url, json={"episodes": 0})
        bad_h = await h.client.post(url, json={"episodes": 5, "horizon": 10})
        ok = await h.client.post(url, json={"episodes": 5, "horizon": 5000})
        again = await h.client.post(url, json={"episodes": 5})
        running = (await h.client.get(f"/experiments/{A}/{eid}")).json()
        h.jobs.finish(ok.json()["execution"])
        back = (await h.client.get(f"/experiments/{A}/{eid}")).json()
        return eid, early, bad, bad_h, ok, again, running, back

    eid, early, bad, bad_h, ok, again, running, back = run(go)
    assert early.status_code == 409
    assert early.json()["detail"] == {
        "reason": "not_ready",
        "message": "traffic can only start when the experiment is ready",
        "status": "deploying",
    }
    assert bad.status_code == 400 and bad_h.status_code == 400
    assert ok.status_code == 200
    assert ok.json()["status"] == "running_traffic"
    (job,) = h.jobs.runs
    assert job == {
        "experiment_id": eid,
        "config_uri": f"gs://bkt/bandit/{eid}/experiment.json",
        "endpoint_id": h.store.rows[eid]["endpoint_id"],
        "episodes": 5,
        "horizon": 5000,
        "execution": ok.json()["execution"],
    }
    assert again.status_code == 409
    assert running["status"] == "running_traffic"
    assert running["trafficExecution"] == ok.json()["execution"]
    assert running["progress"] == {"episodesDone": 0, "episodesTotal": 5}
    assert back["status"] == "ready"


def test_stop_tears_down():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        endpoint = h.store.rows[eid]["endpoint_id"]
        stop = await h.client.post(f"/experiments/{A}/{eid}/stop")
        again = await h.client.post(f"/experiments/{A}/{eid}/stop")
        return eid, endpoint, stop, again

    eid, endpoint, stop, again = run(go)
    assert stop.json() == {"status": "stopped"}
    assert again.json() == {"status": "stopped"}
    assert endpoint not in h.deployer.endpoints
    assert h.deployer.teardowns == [endpoint]
    assert h.store.rows[eid]["stopped_at"] is not None


def test_stop_while_deploying_cleans_up_late_resources():
    h = Harness(wait=0.0)
    h.deployer.gate = asyncio.Event()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await asyncio.sleep(0)
        stop = await h.client.post(f"/experiments/{A}/{eid}/stop")
        h.deployer.gate.set()
        await ex.drain()
        return eid, stop

    eid, stop = run(go)
    assert stop.json()["status"] in ("stopping", "stopped")
    assert h.store.rows[eid]["status"] == "stopped"
    assert not h.deployer.endpoints  # the endpoint created after the stop is gone


def test_reaper_expires_past_ttl():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=3)
        expired = await ex.reap_expired(now=later)
        nothing = await ex.reap_expired(now=later)
        return eid, expired, nothing

    eid, expired, nothing = run(go)
    assert expired == [eid] and nothing == []
    assert h.store.rows[eid]["status"] == "expired"
    assert not h.deployer.endpoints


def test_get_reconciles_ttl_to_expired():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        h.store.rows[eid]["ttl_expires_at"] = dt.datetime.now(dt.UTC)
        return (await h.client.get(f"/experiments/{A}/{eid}")).json()

    detail = run(go)
    assert detail["status"] == "expired"
    assert not h.deployer.endpoints


def test_reconcile_missing_endpoint_marks_failed():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        h.deployer.drop(h.store.rows[eid]["endpoint_id"])
        detail = (await h.client.get(f"/experiments/{A}/{eid}")).json()
        traffic = await h.client.post(
            f"/experiments/{A}/{eid}/traffic", json={"episodes": 1}
        )
        return detail, traffic

    detail, traffic = run(go)
    assert detail["status"] == "failed"
    assert "endpoint" in detail["error"]
    assert traffic.status_code == 409


def test_deploy_failure_marks_failed():
    h = Harness(deployer=FakeDeployer(fail_at="deployed_model_id"))

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        return eid, (await h.client.get(f"/experiments/{A}/{eid}")).json()

    eid, detail = run(go)
    assert detail["status"] == "failed"
    assert "deploy failed" in detail["error"]
    assert not h.deployer.endpoints  # partial resources cleaned up


def test_deploy_resume_is_idempotent():
    """A row left 'deploying' with a model already recorded (api restarted
    mid-deploy) resumes without re-uploading the model."""
    h = Harness()

    async def go():
        await h.session()
        h.deployer.gate = asyncio.Event()
        eid = (await h.create()).json()["experimentId"]
        for _ in range(5):
            await asyncio.sleep(0)
        # simulate the api dying: forget the live task, keep the persisted ids
        task = ex._DEPLOY_TASKS.pop(eid)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        recorded = dict(h.store.rows[eid])
        h.deployer.gate.set()
        await ex.reap_expired()  # the reaper pass resumes orphaned deploys
        await ex.drain()
        return eid, recorded

    eid, recorded = run(go)
    assert recorded["status"] == "deploying"
    assert recorded["model_resource"] and recorded["endpoint_id"]
    row = h.store.rows[eid]
    assert row["status"] == "ready"
    assert row["model_resource"] == recorded["model_resource"]
    assert h.deployer.created["model_resource"] == 1
    assert h.deployer.created["endpoint_id"] == 1


def test_metrics_empty_then_aggregated():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        empty = (await h.client.get(f"/experiments/{A}/{eid}/metrics")).json()
        cid = h.store.rows[eid]["arms"][0]["creativeId"]
        h.store.metrics[eid] = [
            {
                "experiment_id": eid,
                "episode": ep,
                "policy": pol,
                "horizon": 1000,
                "total_reward": 10.0 + ep,
                "curve": json.dumps(
                    {
                        "checkpoints": [10, 100],
                        "cum_avg_reward": [0.1, 0.2],
                        "cum_regret": [1.0, 2.0],
                        "pct_optimal": [0.5, 0.9],
                    }
                ),
                "arm_share": json.dumps({cid: [0.5, 0.9]}),
                "per_segment": "{}",
                "arm_stats": json.dumps(
                    {cid: {"impressions": 10, "clicks": 1, "true_ctr": 0.1}}
                ),
            }
            for ep in (0, 1)
            for pol in ("oracle", "linear_ts")
        ]
        full = (await h.client.get(f"/experiments/{A}/{eid}/metrics")).json()
        return eid, empty, full

    eid, empty, full = run(go)
    assert empty["episodes"] == 0 and empty["experimentId"] == eid
    assert full["episodes"] == 2
    assert full["policies"] == ["linear_ts", "oracle"]
    assert full["checkpoints"] == [10, 100]
    assert full["totals"]["linear_ts"]["mean"] == pytest.approx(10.5)
    assert full["arms"][0]["impressions"] == 20


def test_enforce_mode_foreign_users():
    h = Harness(mode=AuthzMode.ENFORCE)
    alice = {"authorization": "Bearer proxy", "x-tt-user": A}
    bob = {"authorization": "Bearer proxy", "x-tt-user": B}

    async def go():
        await h.session()
        await h.session(user=B, sid="sb")
        spoof = await h.create(user=B, sid="sb", headers=alice)
        anon = await h.create()
        r = await h.create(headers=alice)
        eid = r.json()["experimentId"]
        await ex.drain()
        c = h.client
        return {
            "spoof": spoof,
            "anon": anon,
            "create": r,
            "bob_path": await c.get(f"/experiments/{A}/{eid}", headers=bob),
            "bob_own_path": await c.get(f"/experiments/{B}/{eid}", headers=bob),
            "bob_stop": await c.post(f"/experiments/{B}/{eid}/stop", headers=bob),
            "bob_list": await c.get(f"/experiments/{B}", headers=bob),
            "alice": await c.get(f"/experiments/{A}/{eid}", headers=alice),
            "no_user": await c.get(f"/experiments/{A}/{eid}"),
        }

    r = run(go)
    assert r["spoof"].status_code == 403
    assert r["anon"].status_code == 401
    assert r["create"].status_code == 201
    assert r["bob_path"].status_code == 403  # middleware: path user != trusted
    assert r["bob_own_path"].status_code == 404  # handler: not bob's experiment
    assert r["bob_stop"].status_code == 404
    assert r["bob_list"].json() == {"experiments": []}
    assert r["alice"].status_code == 200
    assert r["no_user"].status_code == 401
