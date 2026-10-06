"""Continuous learning mode in the api (contracts §11): the traffic body's
``learning``, its validation, job env and run record (B1), and the continuous
``/metrics`` aggregation with the batch-means paired difference (B2)."""

from __future__ import annotations

import json
import math
import random

import pytest

from runserver import experiments as ex
from runserver.batch_means import batch_means_summary, lag1_threshold
from runserver.experiments_jobs import FakeJobsRunner, build_env_overrides
from runserver.experiments_metrics import (
    GHOST_POLICY,
    aggregate_episode_metrics,
    t_critical,
)
from runserver.experiments_series import build_creative_series
from runserver.experiments_store import (
    build_creative_series_sql,
    series_rows_from_events,
)
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


# --- batch means -------------------------------------------------------------------------


def _iid(seed, n=40, mean=5.0):
    rng = random.Random(seed)
    return [mean + rng.gauss(0.0, 1.0) for _ in range(n)]


def test_batch_means_iid_is_ok_with_the_t_interval():
    diffs = _iid(1)
    got = batch_means_summary(diffs)
    assert got["status"] == "ok"
    assert got["segments"] == 40 and got["warmupSegments"] == 20
    kept = diffs[20:]
    mean = sum(kept) / 20
    sd = math.sqrt(sum((d - mean) ** 2 for d in kept) / 19)
    assert t_critical(20) == pytest.approx(2.093)  # df = 19
    half = t_critical(20) * sd / math.sqrt(20)
    assert got["mean"] == pytest.approx(mean)
    assert got["lo"] == pytest.approx(mean - half)
    assert got["hi"] == pytest.approx(mean + half)
    assert got["lo"] < 5.0 < got["hi"]
    assert abs(got["lag1"]) < lag1_threshold(20, 0.2)


def test_batch_means_ar1_is_autocorrelated():
    rng = random.Random(7)
    x, diffs = 0.0, []
    for _ in range(200):
        x = 0.8 * x + rng.gauss(0.0, 1.0)
        diffs.append(3.0 + x)
    got = batch_means_summary(diffs)
    assert got["status"] == "autocorrelated"
    assert got["lag1"] > 0.5
    assert got["lo"] is None and got["hi"] is None
    assert got["mean"] == pytest.approx(sum(diffs[100:]) / 100)


def test_batch_means_trend_is_still_trending():
    rng = random.Random(3)
    diffs = [0.5 * i + rng.gauss(0.0, 1.0) for i in range(40)]
    got = batch_means_summary(diffs)
    assert got["status"] == "still_trending"
    assert got["lo"] is None and got["hi"] is None
    # a noiseless line is trending too; a constant series is ok (zero width)
    line = batch_means_summary([float(i) for i in range(12)])
    assert line["status"] == "still_trending"
    flat = batch_means_summary([2.0] * 12)
    assert flat["status"] == "ok" and flat["lag1"] is None
    assert (flat["mean"], flat["lo"], flat["hi"]) == (2.0, 2.0, 2.0)


def test_batch_means_too_few_segments():
    got = batch_means_summary([1.0, 2.0, 3.0, 4.0])
    assert got["status"] == "too_few_segments"
    assert got["warmupSegments"] == 2 and got["mean"] == pytest.approx(3.5)
    assert got["lo"] is None and got["hi"] is None
    # 9 segments: the warm-up is floor(4.5) = 4, leaving exactly 5 batches
    nine = batch_means_summary([1.0, 9.0, 1.0, 9.0, 5.0, 5.1, 4.9, 5.0, 5.05])
    assert nine["warmupSegments"] == 4 and nine["status"] != "too_few_segments"
    empty = batch_means_summary([])
    assert empty["status"] == "too_few_segments" and empty["mean"] == 0.0


def test_lag1_threshold_needs_significance_with_few_batches():
    assert lag1_threshold(20, 0.2) == pytest.approx(1.96 / math.sqrt(20))
    assert lag1_threshold(400, 0.2) == 0.2


# --- continuous aggregation --------------------------------------------------------------

T = 1000
CPS = [250, 500, 750, 1000]
# per policy, per segment: (reward rate, regret rate, optimal rate, clicks)
RATES = {
    "linear_ts": [(0.10, 0.05, 0.40, 100), (0.13, 0.02, 0.70, 131),
                  (0.14, 0.01, 0.85, 139), (0.145, 0.005, 0.9, 146)],
    "ucb1": [(0.09, 0.06, 0.30, 90), (0.11, 0.04, 0.50, 112),
             (0.12, 0.03, 0.60, 118), (0.125, 0.025, 0.65, 126)],
    "uniform": [(0.08, 0.07, 0.25, 80)] * 4,
    "oracle": [(0.15, 0.0, 1.0, 150)] * 4,
}  # fmt: skip


def _seg_row(policy, seg, rate, extra=None):
    r, g, o, clicks = rate
    return {
        "experiment_id": "e",
        "episode": seg,
        "policy": policy,
        "horizon": T,
        "total_reward": r * T,
        "total_clicks": clicks,
        "cumulative_regret": g * T,
        "pct_optimal": o,
        "curve": json.dumps(
            {
                "checkpoints": CPS,
                "cum_avg_reward": [r] * 4,
                "cum_regret": [g * c for c in CPS],
                "pct_optimal": [o] * 4,
                "segment_start": seg * T,
            }
        ),
        "arm_share": {"aa": [0.5] * 4, "bb": [0.5] * 4},
        "per_segment": {"s1": {"optimal_arm": "aa", "pct_optimal": o,
                               "avg_reward": r, "rounds": T}},
        "arm_stats": {"aa": {"impressions": 500, "clicks": 50,
                             "estimated_ctr": 0.1, "true_ctr": 0.1}},
        "traffic_run": 1,
        **(extra or {}),
    }  # fmt: skip


def _continuous_rows(rates=RATES):
    return [
        _seg_row(p, s, rate) for p, segs in rates.items() for s, rate in enumerate(segs)
    ]


def test_continuous_curves_stitch_with_carried_cumulatives():
    m = aggregate_episode_metrics(_continuous_rows(), "e", learning="continuous")
    assert m["learning"] == "continuous"
    assert m["episodes"] == 4 and m["horizon"] == 4 * T and m["segmentHorizon"] == T
    assert m["segmentStarts"] == [0, 1000, 2000, 3000]
    assert m["checkpoints"] == [s * T + c for s in range(4) for c in CPS]
    for policy, segs in RATES.items():
        curves = m["curves"][policy]
        for key in ("cumAvgReward", "cumRegret", "pctOptimal"):
            band = curves[key]
            assert band["lo"] == band["mean"] == band["hi"]  # no bands
            assert len(band["mean"]) == 16
        reward = regret = optimal = 0.0
        i = 0
        for s, (r, g, o, _) in enumerate(segs):
            for c in CPS:
                n = s * T + c
                assert m["checkpoints"][i] == n
                assert curves["cumAvgReward"]["mean"][i] == pytest.approx(
                    (reward + r * c) / n
                )
                assert curves["cumRegret"]["mean"][i] == pytest.approx(regret + g * c)
                assert curves["pctOptimal"]["mean"][i] == pytest.approx(
                    (optimal + o * c) / n
                )
                i += 1
            reward, regret, optimal = reward + r * T, regret + g * T, optimal + o * T
        # continuity: regret never drops at a segment boundary, and the last point
        # is the whole run's total
        reg = curves["cumRegret"]["mean"]
        assert all(b >= a for a, b in zip(reg, reg[1:], strict=False))
        assert reg[-1] == pytest.approx(regret)
        assert curves["cumAvgReward"]["mean"][-1] == pytest.approx(reward / (4 * T))
    # armShare is the endpoint's segment windows in order (not a mean)
    assert m["armShare"]["aa"] == [0.5] * 16


def test_continuous_curves_stitch_without_segment_start():
    rows = _continuous_rows()
    for row in rows:
        curve = json.loads(row["curve"])
        curve.pop("segment_start")
        row["curve"] = curve
    m = aggregate_episode_metrics(rows, "e", learning="continuous")
    assert m["segmentStarts"] == [0, 1000, 2000, 3000]
    assert m["checkpoints"][4] == 1250


def test_continuous_uses_the_common_segment_prefix():
    rows = _continuous_rows()
    # segment 3 only written for the endpoint so far
    rows = [r for r in rows if r["episode"] < 3 or r["policy"] == "linear_ts"]
    m = aggregate_episode_metrics(rows, "e", learning="continuous")
    assert m["episodes"] == 3 and m["horizon"] == 3 * T
    assert len(m["curves"]["linear_ts"]["cumRegret"]["mean"]) == 12
    assert len(m["checkpoints"]) == 12


def test_continuous_summary_pairs_endpoint_with_best_baseline():
    m = aggregate_episode_metrics(_continuous_rows(), "e", learning="continuous")
    diffs = [100 - 90, 131 - 112, 139 - 118, 146 - 126]
    expected = batch_means_summary([float(d) for d in diffs])
    assert m["continuousSummary"] == {
        "segments": 4,
        "warmupSegments": expected["warmupSegments"],
        "pairedDiff": {
            "policy": "ucb1",  # best baseline by whole-run clicks; never the oracle
            "perSegment": diffs,
            "mean": expected["mean"],
            "lo": None,
            "hi": None,
            "lag1": expected["lag1"],
            "status": "too_few_segments",
        },
    }


def test_continuous_summary_ok_with_enough_segments():
    lin = _iid(1, n=12, mean=20.0)
    rates = {
        "linear_ts": [(0.1, 0.01, 0.9, 100 + round(d)) for d in lin],
        "ucb1": [(0.1, 0.02, 0.6, 100)] * 12,
        "epsilon_greedy": [(0.1, 0.03, 0.5, 99)] * 12,
    }
    m = aggregate_episode_metrics(_continuous_rows(rates), "e", learning="continuous")
    pd = m["continuousSummary"]["pairedDiff"]
    assert pd["policy"] == "ucb1"
    assert pd["perSegment"] == [round(d) for d in lin]
    assert pd["status"] == "ok" and pd["lo"] < pd["mean"] < pd["hi"]


def test_continuous_shift_fields_come_from_the_last_segment():
    sr = [
        {
            "round": 2000,
            "pct_optimal_before": 0.8,
            "pct_optimal_after": 0.5,
            "regret_rate_before": 0.01,
            "regret_rate_after": 0.03,
            "recovery_rounds": 400,
            "continuous": True,
            "index": 0,
            "kind": "demote",
            "end_round": None,
            "segment": None,
            "creative_id": "aa",
            "requested_creative_id": "leader",
            "targets": [],
        }
    ]
    regimes = [
        {"start": 0, "end": 2000, "per_segment": {}, "true_ctr": {"aa": 0.1}},
        {"start": 2000, "end": 4000, "per_segment": {}, "true_ctr": {"aa": 0.05}},
    ]
    rates = {"linear_ts": RATES["linear_ts"], GHOST_POLICY: RATES["ucb1"],
             "ucb1": RATES["ucb1"]}  # fmt: skip
    rows = []
    for p, segs in rates.items():
        for s, rate in enumerate(segs):
            extra = {"shift_response": sr, "regimes": regimes} if s == 3 else None
            rows.append(_seg_row(p, s, rate, extra))
    m = aggregate_episode_metrics(rows, "e", learning="continuous")
    (entry,) = m["shiftResponse"]["linear_ts"]
    assert entry["episodes"] == 1 and entry["round"] == 2000
    assert entry["pctOptimalAfter"] == {"mean": 0.5, "lo": 0.5, "hi": 0.5}
    assert [(g["start"], g["end"]) for g in m["regimes"]] == [(0, 2000), (2000, 4000)]
    assert m["resolvedShifts"][0]["creativeId"] == "aa"
    # whole-run ghost - endpoint totals, no interval
    clicks = sum(c for *_, c in RATES["ucb1"]) - sum(c for *_, c in RATES["linear_ts"])
    reward = sum(r * T for r, *_ in RATES["ucb1"]) - sum(
        r * T for r, *_ in RATES["linear_ts"]
    )
    cost = m["shiftCost"]
    assert cost["episodes"] == 4
    assert cost["clicksPerEpisode"] == {"mean": clicks, "lo": clicks, "hi": clicks}
    assert cost["rewardPerEpisode"]["mean"] == pytest.approx(reward)
    assert cost["rewardPerEpisode"]["lo"] == cost["rewardPerEpisode"]["hi"]
    # the ghost is not a "baseline" for the paired difference
    assert m["continuousSummary"]["pairedDiff"]["policy"] == "ucb1"


def test_per_episode_aggregation_is_unchanged():
    rows = _continuous_rows()
    plain = aggregate_episode_metrics(rows, "e")
    assert aggregate_episode_metrics(rows, "e", learning="per_episode") == plain
    assert {"learning", "continuousSummary", "segmentStarts"}.isdisjoint(plain)
    assert plain["checkpoints"] == CPS and plain["horizon"] == T
    lin_regret = plain["curves"]["linear_ts"]["cumRegret"]
    assert lin_regret["lo"] != lin_regret["hi"]  # per-episode bands remain
    empty = aggregate_episode_metrics([], "e", learning="continuous")
    assert empty["episodes"] == 0 and "continuousSummary" not in empty


def test_metrics_route_passes_the_runs_learning():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        h.store.rows[eid]["traffic_runs"] = [
            {"run": 1, "episodes": 4, "horizon": T, "shifts": []},
            {"run": 2, "episodes": 4, "horizon": T, "shifts": [],
             "learning": "continuous"},
        ]  # fmt: skip
        rows = _continuous_rows()
        h.store.metrics[eid] = [
            *({**r, "experiment_id": eid, "traffic_run": 1} for r in rows),
            *({**r, "experiment_id": eid, "traffic_run": 2} for r in rows),
        ]
        url = f"/experiments/{A}/{eid}/metrics"
        return (
            (await h.client.get(url)).json(),
            (await h.client.get(url, params={"run": "1"})).json(),
        )

    cont, per_ep = run(go)
    assert cont["run"] == 2 and cont["learning"] == "continuous"
    assert cont["continuousSummary"]["pairedDiff"]["policy"] == "ucb1"
    assert per_ep["run"] == 1 and "continuousSummary" not in per_ep


# --- series: one stream over the global round --------------------------------------------


def test_continuous_series_sql_groups_by_window_only():
    sql, params = build_creative_series_sql("p.d.ev", "e1", run=2, continuous=True)
    assert "0 AS episode" in sql and "GROUP BY arm, win" in sql
    assert "GROUP BY arm, episode, win" not in sql
    base_sql, base_params = build_creative_series_sql("p.d.ev", "e1", run=2)
    assert "ev.episode AS episode" in base_sql
    assert "GROUP BY arm, episode, win" in base_sql
    got = {p.name: (p.type_, p.value) for p in params}
    assert got == {p.name: (p.type_, p.value) for p in base_params}
    assert got["traffic_run"] == ("INT64", 2) and got["windows"] == ("INT64", 20)


def _events(segments, horizon, arm_of):
    return [
        {"experiment_id": "e", "episode": s, "round": s * horizon + t,
         "policy": "linear_ts", "arm": arm_of(s), "clicked": 1, "segment": "s1",
         "optimal_arm": "aa", "p_chosen": 0.1, "traffic_run": 1}
        for s in range(segments)
        for t in range(horizon)
    ]  # fmt: skip


def test_continuous_series_from_events_is_one_stream():
    events = _events(2, 100, lambda s: "aa" if s == 0 else "bb")
    rows = series_rows_from_events(events, windows=4, continuous=True)
    assert {r["episode"] for r in rows["series"]} == {0}
    assert rows["series"][0]["horizon"] == 200
    # windows over the global round: aa fills windows 0-1, bb windows 2-3
    assert [(r["arm"], r["win"]) for r in rows["series"]] == [
        ("aa", 0), ("aa", 1), ("bb", 2), ("bb", 3),
    ]  # fmt: skip
    body = build_creative_series(rows["series"], [], [], ["aa", "bb"], windows=4)
    assert body["episodes"] == 1
    by_id = {c["creativeId"]: c for c in body["creatives"]}
    assert by_id["aa"]["share"] == [1.0, 1.0, 0.0, 0.0]
    assert by_id["bb"]["cumClicks"] == [0, 0, 50, 100]
    per_episode = series_rows_from_events(events, windows=4)
    assert {r["episode"] for r in per_episode["series"]} == {0, 1}


def test_creatives_route_continuous_run_uses_whole_run_boundaries():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create(scenario="segment_winners")).json()["experimentId"]
        await ex.drain()
        arms = [a["creativeId"] for a in h.store.rows[eid]["arms"]]
        shift = {"kind": "promote", "at_frac": 0.5, "segment": None,
                 "creative_id": arms[1], "lift_pp": 0.01}  # fmt: skip
        h.store.rows[eid]["traffic_runs"] = [
            {"run": 1, "episodes": 2, "horizon": 100, "shifts": [shift],
             "learning": "continuous"},
        ]  # fmt: skip
        events = _events(2, 100, lambda s: arms[s])
        h.store.add_events(eid, [{**e, "experiment_id": eid} for e in events])
        return arms, (await h.client.get(f"/experiments/{A}/{eid}/creatives")).json()

    arms, body = run(go)
    assert body["episodes"] == 1 and body["horizon"] == 200
    assert [(g["start"], g["end"]) for g in body["regimes"]] == [(0, 100), (100, 200)]
    first, second = body["regimes"]
    imps = [
        {c["creativeId"]: c["impressions"] for c in g["creatives"]}
        for g in (first, second)
    ]
    assert imps[0][arms[0]] == 100 and imps[1][arms[1]] == 100
