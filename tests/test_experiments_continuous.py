"""Continuous learning mode in the api (contracts §11): the traffic body's
``learning``, its validation, job env and run record (B1), and the continuous
``/metrics`` aggregation with the batch-means paired difference (B2)."""

from __future__ import annotations

import pytest

from runserver import experiments as ex
from runserver.experiments_jobs import FakeJobsRunner, build_env_overrides
from tests.test_experiments_api import A, Harness, run

# --- parity with bandit.config --------------------------------------------------------


def test_learning_constants_parity_with_bandit():
    from bandit import config as bc

    assert ex.LEARNING_MODES == bc.LEARNING_MODES
    assert ex.MAX_CONTINUOUS_ROUNDS == bc.MAX_CONTINUOUS_ROUNDS


CONTINUOUS_CASES = [
    (50, 40_000, 100, None),
    (5, 400_000, 100, None),
    (100, 20_000, 100, None),  # exactly the cap
    (100, 20_100, 100, "episodes"),  # 2,010,000 > cap
    (51, 40_000, 100, "episodes"),
    (10, 1_050, 100, "horizon"),
    (10, 1_050, 50, None),
]


@pytest.mark.parametrize(("episodes", "horizon", "batch", "field"), CONTINUOUS_CASES)
def test_continuous_limits_match_bandit(episodes, horizon, batch, field):
    from bandit.config import validate_continuous_run

    if field is None:
        assert ex.validate_learning("continuous", episodes, horizon, batch) == (
            "continuous"
        )
        assert validate_continuous_run(episodes, horizon, batch) == episodes * horizon
        return
    with pytest.raises(ex.LearningError) as exc:
        ex.validate_learning("continuous", episodes, horizon, batch)
    assert exc.value.field == field
    with pytest.raises(ValueError, match=field):
        validate_continuous_run(episodes, horizon, batch)


def test_validate_learning_values():
    assert ex.validate_learning(None, 100, 400_000, 100) == "per_episode"
    # per-episode runs keep the per-episode bounds only (no total cap)
    assert ex.validate_learning("per_episode", 100, 400_000, 100) == "per_episode"
    for bad in ("Continuous", "", "both", 1, True, ["continuous"]):
        with pytest.raises(ex.LearningError) as exc:
            ex.validate_learning(bad, 1, 1000, 100)
        assert exc.value.field == "learning"


# --- job env ----------------------------------------------------------------------------


def _env(**over):
    base = {
        "experiment_id": "e1",
        "config_uri": "c",
        "endpoint_id": "x",
        "episodes": 2,
        "horizon": 1000,
    }
    return {e["name"]: e["value"] for e in build_env_overrides(**{**base, **over})}


def test_env_sets_learning_mode_only_when_continuous():
    assert _env(learning="continuous")["LEARNING_MODE"] == "continuous"
    assert "LEARNING_MODE" not in _env(learning="per_episode")
    assert "LEARNING_MODE" not in _env(learning=None)
    assert "LEARNING_MODE" not in _env()


def test_fake_runner_records_learning():
    jobs = FakeJobsRunner()

    async def go():
        await jobs.run(
            experiment_id="e",
            config_uri="c",
            endpoint_id="x",
            episodes=1,
            learning="continuous",
        )
        await jobs.run(experiment_id="e", config_uri="c", endpoint_id="x", episodes=1)

    run(go)
    assert [r["learning"] for r in jobs.runs] == ["continuous", None]


def test_cloud_runner_passes_learning_mode_env():
    from runserver.experiments_jobs import CloudRunJobsRunner

    seen = {}

    class Jobs:
        def run_job(self, request):
            seen["request"] = request

            class Op:
                class metadata:  # noqa: N801
                    name = "projects/p/locations/r/jobs/j/executions/x1"

            return Op()

    runner = CloudRunJobsRunner(
        "j", project="p", region="r", jobs_client_factory=lambda: Jobs()
    )

    async def go():
        return await runner.run(
            experiment_id="e",
            config_uri="c",
            endpoint_id="x",
            episodes=3,
            horizon=1000,
            learning="continuous",
        )

    assert run(go).endswith("/executions/x1")
    (container,) = seen["request"]["overrides"]["container_overrides"]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["LEARNING_MODE"] == "continuous"


# --- route: validation, record, traffic_runs, summary -----------------------------------


def test_continuous_traffic_run_records_learning_and_sets_env():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        url = f"/experiments/{A}/{eid}/traffic"
        first = await h.client.post(url, json={"episodes": 3, "horizon": 40_000})
        h.jobs.finish(first.json()["execution"])
        await h.client.get(f"/experiments/{A}/{eid}")
        second = await h.client.post(
            url,
            json={"episodes": 10, "horizon": 40_000, "learning": "continuous"},
        )
        detail = (await h.client.get(f"/experiments/{A}/{eid}")).json()
        return eid, first, second, detail

    eid, first, second, detail = run(go)
    assert second.status_code == 200 and second.json()["run"] == 2
    j1, j2 = h.jobs.runs
    assert j1["learning"] == "per_episode" and j2["learning"] == "continuous"
    assert j2["forget"] is False  # forget's default is unchanged (no shifts)
    rec1 = h.writer.files[f"gs://bkt/bandit/{eid}/runs/1.json"]
    rec2 = h.writer.files[f"gs://bkt/bandit/{eid}/runs/2.json"]
    assert rec1["learning"] == "per_episode" and rec1["request"]["learning"] is None
    assert rec2["learning"] == "continuous"
    assert rec2["request"]["learning"] == "continuous"
    stored = h.store.rows[eid]["traffic_runs"]
    assert [r["learning"] for r in stored] == ["per_episode", "continuous"]
    assert [r["learning"] for r in detail["trafficRuns"]] == [
        "per_episode",
        "continuous",
    ]


def test_traffic_rejects_bad_learning_with_400():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        url = f"/experiments/{A}/{eid}/traffic"
        bad_value = await h.client.post(
            url, json={"episodes": 2, "learning": "forever"}
        )
        bad_type = await h.client.post(url, json={"episodes": 2, "learning": 3})
        too_long = await h.client.post(
            url,
            json={"episodes": 100, "horizon": 400_000, "learning": "continuous"},
        )
        straddles = await h.client.post(
            url, json={"episodes": 2, "horizon": 1_050, "learning": "continuous"}
        )
        # no horizon in the body: the experiment default (20k) is what's checked
        default_ok = await h.client.post(
            url, json={"episodes": 100, "learning": "continuous"}
        )
        return eid, bad_value, bad_type, too_long, straddles, default_ok

    eid, bad_value, bad_type, too_long, straddles, default_ok = run(go)
    for res, field in (
        (bad_value, "learning"),
        (bad_type, "learning"),
        (too_long, "episodes"),
        (straddles, "horizon"),
    ):
        assert res.status_code == 400, res.text
        assert res.json()["detail"]["reason"] == "invalid_learning"
        assert res.json()["detail"]["field"] == field
    assert default_ok.status_code == 200
    (job,) = h.jobs.runs
    assert job["learning"] == "continuous" and job["episodes"] == 100


def test_legacy_runs_expose_per_episode_learning():
    row = {
        "experiment_id": "e",
        "status": "ready",
        "traffic_execution": "projects/p/locations/r/jobs/j/executions/x1",
        "progress": {"episodes_done": 5, "episodes_total": 5},
    }
    (legacy,) = ex.traffic_runs_of(row)
    assert legacy["learning"] == "per_episode"
    assert ex.traffic_runs_summary(row)[0]["learning"] == "per_episode"
    # an entry written before learning was recorded
    old = {**row, "traffic_runs": [{"run": 1, "episodes": 2, "horizon": 1000}]}
    assert ex.traffic_runs_of(old)[0]["learning"] == "per_episode"
    assert ex.traffic_runs_summary(old)[0]["learning"] == "per_episode"
    cont = {**row, "traffic_runs": [{"run": 1, "learning": "continuous"}]}
    assert ex.traffic_runs_summary(cont)[0]["learning"] == "continuous"
