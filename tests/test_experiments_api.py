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
        "noise_var": round(2 * 0.00933 - 0.00933**2, 6),  # drift/realistic/engaged
        "exploration_scale": 1.0,
        "propensity_samples": 1000,
        "min_propensity": 0.02,
        "discount": 1.0,
    }
    assert (cfg["horizon"], cfg["batch_size"], cfg["episodes"]) == (20000, 100, 20)
    assert 0 <= cfg["seed"] < 2**31
    assert ex.build_experiment_config("e1", arms, "drift")["seed"] == cfg["seed"]
    # §9: scenario_overrides only when non-empty
    assert "scenario_overrides" not in ex.build_experiment_config(
        "e1", arms, "drift", scenario_overrides={}
    )
    tuned = ex.build_experiment_config(
        "e1", arms, "drift", scenario_overrides={"drift_at_frac": 0.3}
    )
    assert tuned["scenario_overrides"] == {"drift_at_frac": 0.3}
    assert set(tuned) == set(cfg) | {"scenario_overrides"}


@pytest.mark.parametrize("scenario", ["clear_winner", "segment_winners", "drift"])
@pytest.mark.parametrize("ctr_mode", ["demo", "realistic"])
@pytest.mark.parametrize("reward_mode", ["click", "engaged"])
def test_api_noise_var_parity_with_bandit(scenario, ctr_mode, reward_mode):
    """The api duplicates the tiny noise_var rule (no jax/bandit import in
    runserver); it must stay identical to ``bandit.config.scenario_noise_var``."""
    from bandit.config import scenario_noise_var

    expected = scenario_noise_var(scenario, ctr_mode, reward_mode)
    assert ex.default_noise_var(scenario, ctr_mode, reward_mode) == expected
    arms = [{"creativeId": "a", "label": "A"}, {"creativeId": "b", "label": "B"}]
    cfg = ex.build_experiment_config("e1", arms, scenario, ctr_mode, reward_mode)
    assert cfg["policy"]["noise_var"] == expected


def test_api_scenario_segments_parity_with_bandit():
    """runserver duplicates the per-scenario segment counts (contracts §9)."""
    from bandit.config import load_scenario

    assert set(ex.SCENARIO_SEGMENTS) == set(ex.SCENARIOS)
    for name, n in ex.SCENARIO_SEGMENTS.items():
        assert len(load_scenario(name).segments) == n, name


def test_api_override_bounds_parity_with_bandit():
    from bandit.config import OVERRIDE_BOUNDS

    assert ex.OVERRIDE_BOUNDS == OVERRIDE_BOUNDS
    assert set(ex.OVERRIDE_FIELDS.values()) == set(OVERRIDE_BOUNDS)


_FULL_OVERRIDES = {
    "clear_winner": {
        "segmentMix": [0.6, 0.2, 0.2],
        "gapScale": 1.5,
        "judgeWrong": 0.8,
        "noiseScale": 0,
    },
    "segment_winners": {"segmentMix": [0.05, 1, 0.3, 0.3], "gapScale": 0.25},
    "drift": {"driftAtFrac": 0.3, "noiseScale": 2.0, "judgeWrong": 1},
}


@pytest.mark.parametrize("scenario", sorted(_FULL_OVERRIDES))
def test_api_built_config_with_overrides_loads_strictly_in_bandit(scenario):
    """An api-built experiment.json with overrides passes bandit's strict loader."""
    from bandit.config import (
        load_experiment_config,
        scenario_overrides_to_dict,
        validate_experiment_config,
    )

    ov = ex.validate_scenario_overrides(scenario, _FULL_OVERRIDES[scenario])
    arms = [
        {"creativeId": "a", "label": "A", "scores": {"visual_overall": 0.7}},
        {"creativeId": "b", "label": "B", "scores": {"visual_overall": 0.4}},
    ]
    cfg = ex.build_experiment_config("e1", arms, scenario, scenario_overrides=ov)
    loaded = validate_experiment_config(
        load_experiment_config(json.loads(json.dumps(cfg)))
    )
    assert scenario_overrides_to_dict(loaded.scenario_overrides) == ov


def test_validate_scenario_overrides_shapes():
    v = ex.validate_scenario_overrides
    assert v("drift", None) is None
    assert v("drift", {}) is None
    assert v("drift", {"gapScale": None}) is None
    # stored as sent (bandit renormalises), ints become floats
    assert v("clear_winner", {"segmentMix": [1, 1, 1], "judgeWrong": 0}) == {
        "segment_mix": [1.0, 1.0, 1.0],
        "judge_wrong": 0.0,
    }


@pytest.mark.parametrize(
    ("scenario", "ov", "field"),
    [
        ("drift", {"bogus": 1}, "bogus"),
        ("drift", {"gap_scale": 1.0}, "gap_scale"),
        ("drift", {"gapScale": True}, "gapScale"),
        ("drift", {"gapScale": "1.5"}, "gapScale"),
        ("drift", {"gapScale": float("nan")}, "gapScale"),
        ("drift", {"gapScale": float("inf")}, "gapScale"),
        ("drift", {"gapScale": 0.2}, "gapScale"),
        ("drift", {"gapScale": 2.01}, "gapScale"),
        ("drift", {"judgeWrong": -0.1}, "judgeWrong"),
        ("drift", {"judgeWrong": 1.1}, "judgeWrong"),
        ("drift", {"noiseScale": 2.5}, "noiseScale"),
        ("drift", {"driftAtFrac": 0.1}, "driftAtFrac"),
        ("drift", {"driftAtFrac": 0.9}, "driftAtFrac"),
        ("clear_winner", {"driftAtFrac": 0.5}, "driftAtFrac"),
        ("segment_winners", {"driftAtFrac": 0.5}, "driftAtFrac"),
        ("clear_winner", {"segmentMix": [0.5, 0.5]}, "segmentMix"),
        ("segment_winners", {"segmentMix": [0.3, 0.3, 0.4]}, "segmentMix"),
        ("clear_winner", {"segmentMix": [0.5, 0.5, 0.01]}, "segmentMix"),
        ("clear_winner", {"segmentMix": [0.5, 0.5, 1.5]}, "segmentMix"),
        ("clear_winner", {"segmentMix": [0.5, 0.5, False]}, "segmentMix"),
        ("clear_winner", {"segmentMix": "0.3,0.3,0.4"}, "segmentMix"),
        ("clear_winner", {"segmentMix": 0.5}, "segmentMix"),
    ],
)
def test_validate_scenario_overrides_rejects(scenario, ov, field):
    with pytest.raises(ex.ScenarioOverridesError) as e:
        ex.validate_scenario_overrides(scenario, ov)
    assert e.value.field == field


def test_api_policy_override_wins_over_calibrated_noise_var():
    arms = [{"creativeId": "a", "label": "A"}]
    cfg = ex.build_experiment_config("e1", arms, "drift", policy={"noise_var": 0.3})
    assert cfg["policy"]["noise_var"] == 0.3


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
        "endpointId", "trafficExecution", "progress", "error", "scenarioOverrides",
    }  # fmt: skip
    assert s["scenarioOverrides"] is None
    stored = ex.to_summary({**row, "scenario_overrides": '{"gap_scale": 1.5}'})
    assert stored["scenarioOverrides"] == {"gapScale": 1.5}


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


def test_create_with_scenario_overrides_round_trips():
    h = Harness()

    async def go():
        await h.session()
        r = await h.create(
            scenario="drift",
            scenarioOverrides={"driftAtFrac": 0.3, "gapScale": 1.5, "noiseScale": None},
        )
        assert r.status_code == 201, r.text
        eid = r.json()["experimentId"]
        await ex.drain()
        detail = (await h.client.get(f"/experiments/{A}/{eid}")).json()
        listed = (await h.client.get(f"/experiments/{A}")).json()
        return eid, detail, listed

    eid, detail, listed = run(go)
    cfg = h.writer.files[f"gs://bkt/bandit/{eid}/experiment.json"]
    assert cfg["scenario_overrides"] == {"gap_scale": 1.5, "drift_at_frac": 0.3}
    assert h.store.rows[eid]["scenario_overrides"] == cfg["scenario_overrides"]
    want = {"gapScale": 1.5, "driftAtFrac": 0.3}
    assert detail["scenarioOverrides"] == want
    assert listed["experiments"][0]["scenarioOverrides"] == want


def test_create_without_overrides_writes_no_key():
    h = Harness()

    async def go():
        await h.session()
        r = await h.create(scenarioOverrides={})
        assert r.status_code == 201, r.text
        eid = r.json()["experimentId"]
        await ex.drain()
        return eid, (await h.client.get(f"/experiments/{A}/{eid}")).json()

    eid, detail = run(go)
    cfg = h.writer.files[f"gs://bkt/bandit/{eid}/experiment.json"]
    assert "scenario_overrides" not in cfg
    # the column isn't named, so an unmigrated table keeps working
    assert "scenario_overrides" not in h.store.rows[eid]
    assert detail["scenarioOverrides"] is None


def test_create_validation_errors():
    h = Harness()

    async def go():
        await h.session()
        empty = await h.create(indices=[])
        one = await h.create(indices=[1])
        oob = await h.create(indices=[0, 7])
        bad_scenario = await h.create(scenario="nope")
        missing = await h.create(sid="nope")
        bad_ov = [
            await h.create(scenarioOverrides=ov)
            for ov in (
                {"unknownKnob": 1},
                {"segmentMix": [0.5, 0.5]},
                {"gapScale": 9},
                {"judgeWrong": True},
                {"noiseScale": "x"},
                {"driftAtFrac": 0.5},  # clear_winner, not drift
            )
        ]
        return empty, one, oob, bad_scenario, missing, bad_ov

    empty, one, oob, bad_scenario, missing, bad_ov = run(go)
    assert (
        empty.status_code == 400 and empty.json()["detail"]["reason"] == "too_few_arms"
    )
    assert one.status_code == 400
    assert oob.json()["detail"]["reason"] == "index_out_of_range"
    assert bad_scenario.json()["detail"]["reason"] == "invalid_scenario"
    assert missing.status_code == 404
    fields = ["unknownKnob", "segmentMix", "gapScale", "judgeWrong", "noiseScale"]
    for r, field in zip(bad_ov, [*fields, "driftAtFrac"], strict=True):
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["reason"] == "invalid_scenario_overrides"
        assert detail["field"] == field
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


def test_concurrent_deploy_attempts_upload_once():
    """Two deploy attempts for one experiment (e.g. the live revision and an old
    one's reaper) race: the lease lets exactly one of them deploy."""
    h = Harness()

    async def go():
        await h.session()
        h.deployer.gate = asyncio.Event()
        eid = (await h.create()).json()["experimentId"]
        # bypass the in-process task guard, as a second instance would
        racers = [asyncio.create_task(ex._deploy_task(eid)) for _ in range(2)]
        await ex.reap_expired()
        await h.client.get(f"/experiments/{A}/{eid}")
        for _ in range(5):
            await asyncio.sleep(0)
        h.deployer.gate.set()
        await asyncio.gather(*racers)
        await ex.drain()
        return eid

    eid = run(go)
    row = h.store.rows[eid]
    assert row["status"] == "ready"
    assert h.deployer.created == {
        "model_resource": 1,
        "endpoint_id": 1,
        "deployed_model_id": 1,
    }
    assert row["deploy_lease_until"] is None  # released when the deploy finished
    assert row["deploy_lease_owner"] is None


def test_foreign_lease_blocks_resume_until_it_expires():
    h = Harness()

    async def go():
        await h.session()
        h.deployer.gate = asyncio.Event()
        eid = (await h.create()).json()["experimentId"]
        task = ex._DEPLOY_TASKS.pop(eid)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # another (live) instance holds the lease
        row = h.store.rows[eid]
        row["deploy_lease_owner"] = "other-rev/1234"
        row["deploy_lease_until"] = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=3)
        h.deployer.gate.set()
        await ex.reap_expired()
        await h.client.get(f"/experiments/{A}/{eid}")
        await ex._deploy_task(eid)  # even a direct attempt backs off
        await ex.drain()
        blocked = (dict(row), dict(h.deployer.created))
        # the holder died: its lease expires and the next reaper pass resumes
        row["deploy_lease_until"] = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await ex.reap_expired()
        await ex.drain()
        return eid, blocked

    eid, (blocked_row, blocked_created) = run(go)
    assert blocked_row["status"] == "deploying"
    assert blocked_created["deployed_model_id"] == 0
    assert h.store.rows[eid]["status"] == "ready"
    assert h.deployer.created["model_resource"] == 1


def test_deploy_lease_heartbeat_extends_and_failure_releases():
    h = Harness(deployer=FakeDeployer(fail_at="deployed_model_id"))
    h.settings.deploy_heartbeat_seconds = 0.01

    async def go():
        await h.session()
        h.deployer.gate = asyncio.Event()
        eid = (await h.create()).json()["experimentId"]
        for _ in range(5):
            await asyncio.sleep(0)
        row = h.store.rows[eid]
        first = row["deploy_lease_until"]
        owner = row["deploy_lease_owner"]
        await asyncio.sleep(0.05)
        renewed = row["deploy_lease_until"]
        h.deployer.gate.set()
        await ex.drain()
        return eid, first, renewed, owner

    eid, first, renewed, owner = run(go)
    assert owner == ex.LEASE_OWNER and first is not None
    assert renewed > first  # the heartbeat pushed the expiry out
    row = h.store.rows[eid]
    assert row["status"] == "failed"
    assert row["deploy_lease_until"] is None and row["deploy_lease_owner"] is None


def test_deploy_reuses_labelled_resources_when_row_lost_ids():
    """Second layer: even without the lease, a resumed deploy whose progress
    writes were lost adopts the labelled model/endpoint instead of re-uploading."""
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        row = h.store.rows[eid]
        row.update(status="deploying", model_resource=None, endpoint_id=None)
        row["deployed_model_id"] = None
        await ex.reap_expired()
        await ex.drain()
        return eid

    eid = run(go)
    assert h.store.rows[eid]["status"] == "ready"
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


def test_creative_series_owner_empty_populated_and_cache():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create(rewardMode="engaged")).json()["experimentId"]
        await ex.drain()
        c = h.client
        foreign = await c.get(f"/experiments/{B}/{eid}/creatives")
        missing = await c.get(f"/experiments/{A}/ffffffffffffffff/creatives")
        empty = (await c.get(f"/experiments/{A}/{eid}/creatives")).json()
        a0, a1 = (a["creativeId"] for a in h.store.rows[eid]["arms"])
        h.store.add_events(
            eid,
            [
                {
                    "policy": "linear_ts",
                    "episode": ep,
                    "round": r,
                    "arm": a1 if r >= 20 or r % 2 else a0,
                    "clicked": int(r % 4 == 0),
                    "dwell_s": 10.0 if r % 4 == 0 else None,
                    "segment": "seg",
                    "optimal_arm": a1,
                    "p_chosen": 0.05,
                    "regret": 0.0 if r >= 20 or r % 2 else 0.02,
                }
                for ep in (0, 1)
                for r in range(40)
            ],
        )
        # "ready" is not cached, so the new events show up immediately.
        full = (await c.get(f"/experiments/{A}/{eid}/creatives")).json()
        await h.store.upsert({**h.store.rows[eid], "status": "stopped"})
        stopped = (await c.get(f"/experiments/{A}/{eid}/creatives")).json()
        h.store.add_events(
            eid, [{"policy": "linear_ts", "episode": 9, "round": 0, "arm": a0}]
        )
        cached = (await c.get(f"/experiments/{A}/{eid}/creatives")).json()
        return eid, (a0, a1), foreign, missing, empty, full, stopped, cached

    eid, (a0, a1), foreign, missing, empty, full, stopped, cached = run(go)
    assert foreign.status_code == 404 and missing.status_code == 404
    assert foreign.json()["detail"]["reason"] == "not_found"
    assert empty["experimentId"] == eid and empty["episodes"] == 0
    assert empty["horizon"] is None and empty["windows"] == []
    assert [c["creativeId"] for c in empty["creatives"]] == [a0, a1]
    assert all(c["share"] == [] for c in empty["creatives"])
    assert all(
        c["segments"] == [] and c["missedClicks"] == 0 for c in empty["creatives"]
    )
    assert all(c["engagedSecondsPer1k"] is None for c in empty["creatives"])
    assert full["episodes"] == 2 and full["horizon"] == 40
    assert len(full["windows"]) == 20
    assert [c["creativeId"] for c in full["creatives"]] == [a1, a0]
    top = full["creatives"][0]
    assert top["finalShare"] == 1.0 and top["segmentsWon"] == ["seg"]
    assert full["creatives"][1]["ctr"][-1] is None
    # a0: rounds 0,2,..,18 per episode (all clicked on r % 4 == 0 -> 5 clicks)
    low = full["creatives"][1]
    assert low["segments"] == [
        {
            "segment": "seg",
            "impressions": 20,
            "clicks": 10,
            "ctr": 0.5,
            "trueCtr": 0.05,
            "isBest": False,
        }
    ]
    assert low["missedClicks"] == pytest.approx(0.2)  # 10 rows x 0.02 per ep
    assert low["engagedSecondsPer1k"] == pytest.approx(1000 * 100 / 20)
    assert top["segments"][0]["isBest"] is True and top["missedClicks"] == 0
    assert top["segments"][0]["impressions"] == 60
    assert stopped == full
    assert cached == stopped  # stopped -> cached indefinitely


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
