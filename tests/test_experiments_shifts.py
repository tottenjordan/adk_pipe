"""Scripted behaviour shifts in the api (contracts §10, PR C): shift validation
and its parity with ``bandit.config``, numbered traffic runs, ``?run=`` reads,
``shift_response`` / regime aggregation and the regime SQL builder.

runserver never imports ``bandit``; only these tests do (parity)."""

from __future__ import annotations

import asyncio
import json
import math
import re

import numpy as np
import pytest

from runserver import experiments as ex
from runserver.experiments_metrics import (
    GHOST_POLICY,
    aggregate_episode_metrics,
    order_policies,
)
from runserver.experiments_series import build_creative_series
from runserver.experiments_store import (
    BigQueryExperimentStore,
    InMemoryExperimentStore,
    build_creative_series_sql,
    build_metrics_sql,
    build_regimes_sql,
    series_rows_from_events,
)
from tests._fake_bq import FakeBigQueryClient
from tests.test_experiments_api import A, Harness, run

ARMS = [{"creativeId": "aa", "label": "A"}, {"creativeId": "bb", "label": "B"}]
SEG = "segment_winners"

VALID = [
    {
        "kind": "promote",
        "atFrac": 0.4,
        "segment": "mobile_scrollers",
        "creativeId": "bb",
        "liftPp": 0.015,
    },
    {
        "kind": "demote",
        "atFrac": 0.5,
        "segment": None,
        "creativeId": "leader",
        "dropPp": 0.01,
    },
    {"kind": "mix", "atFrac": 0.3, "segmentMix": [0.6, 0.2, 0.1, 0.1]},
    {
        "kind": "shock",
        "atFrac": 0.6,
        "untilFrac": 0.7,
        "segment": None,
        "creativeId": "leader",
        "ctrMultiplier": 0.6,
    },
]


def _one(**over):
    """A valid promote shift with ``over`` applied (``None`` values kept)."""
    return [{**VALID[0], **over}]


# --- parity with bandit.config ------------------------------------------------------


def test_shift_constants_parity_with_bandit():
    from bandit import config as bc

    assert ex.SHIFT_KINDS == bc.SHIFT_KINDS
    assert ex.SHIFT_BOUNDS == bc.SHIFT_BOUNDS
    assert ex.MAX_SHIFTS == bc.MAX_SHIFTS
    assert ex.SHIFT_MIN_WINDOW == bc.SHIFT_MIN_WINDOW
    assert ex.LEADER == bc.LEADER
    assert ex.LEADER_KINDS == bc.LEADER_KINDS
    assert ex.SHIFT_KIND_FIELDS == bc._SHIFT_FIELDS
    assert ex.SHIFT_REQUIRED == bc._SHIFT_REQUIRED
    import dataclasses

    assert list(ex.SHIFT_FIELDS.values()) == [
        f.name for f in dataclasses.fields(bc.ShiftSpec)
    ]


def test_scenario_segment_names_and_ctr_scale_parity_with_bandit():
    from bandit.config import CTR_MODES, load_scenario

    assert set(ex.SCENARIO_SEGMENT_NAMES) == set(ex.SCENARIOS)
    for name, names in ex.SCENARIO_SEGMENT_NAMES.items():
        sc = load_scenario(name)
        assert tuple(s.name for s in sc.segments) == names, name
        for mode in CTR_MODES:
            assert ex.ctr_scale(name, mode) == pytest.approx(sc.ctr_scale(mode))


def _bandit_check(scenario, ctr_mode, arms, snake):
    from bandit.config import load_scenario, shifts_from_dict, shifts_to_dict
    from bandit.config import validate_shifts as bandit_validate

    specs = bandit_validate(
        shifts_from_dict(snake), load_scenario(scenario), arms, ctr_mode
    )
    return shifts_to_dict(specs)


@pytest.mark.parametrize("ctr_mode", ["demo", "realistic"])
def test_valid_shifts_match_bandit_round_trip(ctr_mode):
    scale = ex.ctr_scale(SEG, ctr_mode)
    shifts = json.loads(json.dumps(VALID))
    shifts[0]["liftPp"] *= scale
    shifts[1]["dropPp"] *= scale
    out = ex.validate_shifts(SEG, ctr_mode, ARMS, shifts)
    assert out == _bandit_check(SEG, ctr_mode, ["aa", "bb"], out)
    assert out[1] == {
        "kind": "demote",
        "at_frac": 0.5,
        "segment": None,
        "creative_id": "leader",
        "drop_pp": 0.01 * scale,
    }
    assert out[2] == {
        "kind": "mix",
        "at_frac": 0.3,
        "segment_mix": [0.6, 0.2, 0.1, 0.1],
    }


def _camel_to_snake_field(field: str) -> str:
    m = re.fullmatch(r"(shifts\[\d+\])\.(\w+)", field)
    if not m:
        return field
    return f"{m.group(1)}.{ex.SHIFT_FIELDS.get(m.group(2), m.group(2))}"


# (scenario, ctr_mode, shifts, field) -- one error each.
INVALID = [
    (SEG, "demo", "nope", "shifts"),
    (SEG, "demo", [VALID[2]] * 5, "shifts"),
    (SEG, "demo", ["x"], "shifts[0]"),
    (SEG, "demo", _one(kind="spin"), "shifts[0].kind"),
    (SEG, "demo", _one(kind=None), "shifts[0].kind"),
    (SEG, "demo", _one(bogus=1), "shifts[0].bogus"),
    (SEG, "demo", _one(atFrac=None), "shifts[0].atFrac"),
    (SEG, "demo", _one(atFrac=0.04), "shifts[0].atFrac"),
    (SEG, "demo", _one(atFrac=0.96), "shifts[0].atFrac"),
    (SEG, "demo", _one(atFrac=True), "shifts[0].atFrac"),
    (SEG, "demo", _one(atFrac="0.4"), "shifts[0].atFrac"),
    (SEG, "demo", _one(atFrac=math.nan), "shifts[0].atFrac"),
    (SEG, "demo", _one(atFrac=math.inf), "shifts[0].atFrac"),
    (SEG, "demo", _one(liftPp=0.004), "shifts[0].liftPp"),
    (SEG, "demo", _one(liftPp=0.031), "shifts[0].liftPp"),
    (SEG, "demo", _one(liftPp=False), "shifts[0].liftPp"),
    (SEG, "demo", _one(liftPp=None), "shifts[0].liftPp"),
    (SEG, "realistic", _one(liftPp=0.015), "shifts[0].liftPp"),  # max 0.006
    (SEG, "realistic", _one(liftPp=0.0009), "shifts[0].liftPp"),
    (SEG, "demo", _one(dropPp=0.01), "shifts[0].dropPp"),  # another kind's field
    (SEG, "demo", _one(segment="commuters"), "shifts[0].segment"),
    (SEG, "demo", _one(segment=3), "shifts[0].segment"),
    (SEG, "demo", _one(creativeId="zz"), "shifts[0].creativeId"),
    (SEG, "demo", _one(creativeId="leader"), "shifts[0].creativeId"),  # promote
    (SEG, "demo", _one(creativeId=None), "shifts[0].creativeId"),
    (SEG, "demo", [{**VALID[1], "dropPp": 0.0301}], "shifts[0].dropPp"),
    (SEG, "demo", [{**VALID[1], "dropPp": 0.0049}], "shifts[0].dropPp"),
    (SEG, "demo", [{**VALID[1], "creativeId": "zz"}], "shifts[0].creativeId"),
    (SEG, "demo", [{**VALID[2], "segmentMix": [0.5, 0.5]}], "shifts[0].segmentMix"),
    (
        SEG,
        "demo",
        [{**VALID[2], "segmentMix": [0.04, 0.5, 0.5, 0.5]}],
        "shifts[0].segmentMix",
    ),
    (
        SEG,
        "demo",
        [{**VALID[2], "segmentMix": [1.01, 0.5, 0.5, 0.5]}],
        "shifts[0].segmentMix",
    ),
    (
        SEG,
        "demo",
        [{**VALID[2], "segmentMix": [True, 0.5, 0.5, 0.5]}],
        "shifts[0].segmentMix",
    ),
    (SEG, "demo", [{**VALID[2], "segmentMix": "0.5"}], "shifts[0].segmentMix"),
    (SEG, "demo", [{**VALID[2], "segment": "mobile_scrollers"}], "shifts[0].segment"),
    ("clear_winner", "demo", [{**VALID[2]}], "shifts[0].segmentMix"),  # 3 segments
    (SEG, "demo", [{**VALID[3], "untilFrac": 0.61}], "shifts[0].untilFrac"),  # window
    (SEG, "demo", [{**VALID[3], "untilFrac": 1.01}], "shifts[0].untilFrac"),
    (SEG, "demo", [{**VALID[3], "untilFrac": None}], "shifts[0].untilFrac"),
    (SEG, "demo", [{**VALID[3], "ctrMultiplier": 0.29}], "shifts[0].ctrMultiplier"),
    (SEG, "demo", [{**VALID[3], "ctrMultiplier": 2.01}], "shifts[0].ctrMultiplier"),
    (SEG, "demo", [VALID[0], {**VALID[3], "creativeId": "zz"}], "shifts[1].creativeId"),
]


@pytest.mark.parametrize(("scenario", "ctr_mode", "shifts", "field"), INVALID)
def test_invalid_shifts_name_the_field_and_bandit_agrees(
    scenario, ctr_mode, shifts, field
):
    with pytest.raises(ex.ShiftsError) as err:
        ex.validate_shifts(scenario, ctr_mode, ARMS, shifts)
    assert err.value.field == field
    # bandit (the source of truth) rejects the snake_case form too, naming the same
    # field (the list-level / type errors aside, where bandit's message differs).
    snake = (
        [
            {ex.SHIFT_FIELDS.get(k, k): v for k, v in s.items()}
            if isinstance(s, dict)
            else s
            for s in shifts
        ]
        if isinstance(shifts, list)
        else shifts
    )
    with pytest.raises(ValueError) as bandit_err:
        _bandit_check(scenario, ctr_mode, ["aa", "bb"], snake)
    if "." in field:
        assert _camel_to_snake_field(field) in str(bandit_err.value)


def test_shift_boundary_values_are_inclusive():
    for kw in (
        {"atFrac": 0.05, "liftPp": 0.005},
        {"atFrac": 0.95, "liftPp": 0.03},
    ):
        assert ex.validate_shifts(SEG, "demo", ARMS, _one(**kw))
    real = ex.validate_shifts(SEG, "realistic", ARMS, _one(liftPp=0.006))
    assert real[0]["lift_pp"] == 0.006
    shock = {**VALID[3], "atFrac": 0.05, "untilFrac": 0.07, "ctrMultiplier": 2.0}
    assert ex.validate_shifts(SEG, "demo", ARMS, [shock])[0]["until_frac"] == 0.07
    assert ex.validate_shifts(SEG, "demo", ARMS, None) == []
    assert ex.validate_shifts(SEG, "demo", ARMS, []) == []
    # null optional fields count as unset; segment null is kept on kinds that take it
    (promote,) = ex.validate_shifts(SEG, "demo", ARMS, _one(segment=None))
    assert promote["segment"] is None and "drop_pp" not in promote


def test_shift_boundaries_and_camel_round_trip():
    snake = ex.validate_shifts(SEG, "demo", ARMS, VALID)
    assert ex.shift_boundaries(snake, 40_000) == [
        12_000,
        16_000,
        20_000,
        24_000,
        28_000,
    ]
    assert ex.shift_boundaries([], 40_000) == []
    assert ex.shift_boundaries(snake, None) == []
    camel = ex.shifts_to_camel(snake)
    assert ex.validate_shifts(SEG, "demo", ARMS, camel) == snake


def test_shift_boundaries_match_bandit_resolution():
    """The api's regime boundaries equal bandit's resolved shift / shock-end rounds."""
    from bandit import simulate
    from bandit.config import build_sim_config, default_arms, shifts_from_dict

    arms = default_arms(2)
    ids = [a.creative_id for a in arms]
    camel = [
        {**VALID[0], "creativeId": ids[1], "atFrac": 0.333},
        {**VALID[3], "creativeId": ids[0], "atFrac": 0.5, "untilFrac": 0.7777},
    ]
    snake = ex.validate_shifts(SEG, "demo", ids, camel)
    cfg = build_sim_config(SEG, horizon=12_345, arms=arms)
    env = simulate.build_environment(cfg, shifts=shifts_from_dict(snake))
    from bandit.environment import resolved_shifts

    rounds = set()
    for rec in resolved_shifts(env):
        rounds.add(rec["round"])
        if rec.get("end_round") is not None:
            rounds.add(rec["end_round"])
    assert ex.shift_boundaries(snake, 12_345) == sorted(rounds)


# --- traffic runs through the REST api -----------------------------------------------


def _summary_runs(h, eid):
    return ex.to_summary(h.store.rows[eid])["trafficRuns"]


def test_traffic_runs_allocate_write_records_and_pass_env():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create(scenario=SEG)).json()["experimentId"]
        await ex.drain()
        url = f"/experiments/{A}/{eid}/traffic"
        arms = [a["creativeId"] for a in h.store.rows[eid]["arms"]]
        shifts = [
            {**VALID[1], "creativeId": "leader"},
            {**VALID[3], "creativeId": arms[0]},
        ]
        first = await h.client.post(url, json={"episodes": 3, "horizon": 40_000})
        h.jobs.finish(first.json()["execution"])
        await h.client.get(f"/experiments/{A}/{eid}")
        second = await h.client.post(
            url, json={"episodes": 2, "horizon": 40_000, "shifts": shifts}
        )
        detail = (await h.client.get(f"/experiments/{A}/{eid}")).json()
        h.jobs.finish(second.json()["execution"])
        await h.client.get(f"/experiments/{A}/{eid}")
        third = await h.client.post(
            url, json={"episodes": 1, "shifts": shifts, "forget": False}
        )
        done = (await h.client.get(f"/experiments/{A}/{eid}")).json()
        return eid, arms, shifts, first, second, third, detail, done

    eid, arms, shifts, first, second, third, detail, done = run(go)
    assert [r.json()["run"] for r in (first, second, third)] == [1, 2, 3]
    j1, j2, j3 = h.jobs.runs
    assert (j1["traffic_run"], j1["forget"], j1["shifts"]) == (1, False, None)
    assert (j2["traffic_run"], j2["forget"]) == (2, True)  # default on with shifts
    assert j2["shifts"] == ex.validate_shifts(SEG, "demo", arms, shifts)
    assert (j3["traffic_run"], j3["forget"]) == (3, False)
    rec = h.writer.files[f"gs://bkt/bandit/{eid}/runs/2.json"]
    assert rec["run"] == 2 and rec["episodes"] == 2 and rec["horizon"] == 40_000
    assert rec["forget"] is True and rec["shifts"] == j2["shifts"]
    assert rec["request"]["shifts"] == shifts and rec["request"]["forget"] is None
    assert rec["started_at"].endswith("Z")
    # no horizon in the body: the run records the experiment's default horizon
    assert h.writer.files[f"gs://bkt/bandit/{eid}/runs/3.json"]["horizon"] == 20_000
    running = detail["trafficRuns"]
    assert [r["status"] for r in running] == ["finished", "running"]
    assert running[1]["shifts"] == ex.shifts_to_camel(j2["shifts"])
    assert running[1]["forget"] is True and running[1]["episodes"] == 2
    assert set(running[1]) == {
        "run", "startedAt", "episodes", "horizon", "shifts", "forget", "status",
    }  # fmt: skip
    assert [r["run"] for r in done["trafficRuns"]] == [1, 2, 3]
    stored = h.store.rows[eid]["traffic_runs"]
    assert [r["execution"] for r in stored] == [
        first.json()["execution"],
        second.json()["execution"],
        third.json()["execution"],
    ]


def test_traffic_rejects_bad_shifts_and_forget_with_400():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create(scenario=SEG)).json()["experimentId"]
        await ex.drain()
        url = f"/experiments/{A}/{eid}/traffic"
        bad_creative = await h.client.post(
            url, json={"episodes": 2, "shifts": [{**VALID[0], "creativeId": "zz"}]}
        )
        bad_window = await h.client.post(
            url,
            json={
                "episodes": 2,
                "shifts": [VALID[2], {**VALID[3], "untilFrac": 0.61}],
            },
        )
        bad_forget = await h.client.post(url, json={"episodes": 2, "forget": "yes"})
        return eid, bad_creative, bad_window, bad_forget

    eid, bad_creative, bad_window, bad_forget = run(go)
    assert bad_creative.status_code == 400
    assert bad_creative.json()["detail"]["reason"] == "invalid_shifts"
    assert bad_creative.json()["detail"]["field"] == "shifts[0].creativeId"
    assert bad_window.json()["detail"]["field"] == "shifts[1].untilFrac"
    assert bad_forget.status_code == 400
    assert bad_forget.json()["detail"]["reason"] == "invalid_forget"
    assert h.jobs.runs == [] and h.writer.files.keys() == {
        f"gs://bkt/bandit/{eid}/experiment.json"
    }


def test_legacy_experiment_gets_a_synthesized_run_one():
    row = {
        "experiment_id": "e",
        "status": "ready",
        "traffic_execution": "projects/p/locations/r/jobs/j/executions/x1",
        "progress": {"episodes_done": 5, "episodes_total": 5},
    }
    (legacy,) = ex.traffic_runs_of(row)
    assert legacy["run"] == 1 and legacy["episodes"] == 5 and legacy["shifts"] == []
    assert ex.latest_run(row) == 1
    assert ex.traffic_runs_summary(row) == [
        {
            "run": 1,
            "startedAt": None,
            "episodes": 5,
            "horizon": None,
            "shifts": [],
            "forget": False,
            "status": "finished",
        }
    ]
    assert ex.latest_run({"experiment_id": "e"}) == 1
    assert ex.traffic_runs_of({"traffic_runs": "not json"}) == []
    cut = {**row, "progress": {"episodes_done": 2, "episodes_total": 5}}
    assert ex.traffic_runs_summary(cut)[0]["status"] == "finished"  # progress lag
    stopped = {**cut, "status": "stopped"}
    assert ex.traffic_runs_summary(stopped)[0]["status"] == "stopped"
    assert ex.traffic_runs_summary({**row, "error": "x"})[0]["status"] == "failed"


def test_next_run_after_legacy_traffic_is_two():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        await h.store.upsert(
            {
                **h.store.rows[eid],
                "traffic_execution": "projects/p/locations/r/jobs/j/executions/old",
                "progress": {"episodes_done": 4, "episodes_total": 4},
            }
        )
        res = await h.client.post(
            f"/experiments/{A}/{eid}/traffic", json={"episodes": 1}
        )
        return eid, res

    eid, res = run(go)
    assert res.json()["run"] == 2
    legacy, new = h.store.rows[eid]["traffic_runs"]
    assert legacy["run"] == 1 and legacy["status"] == "finished"
    assert new["run"] == 2 and new["shifts"] == []


# --- ?run= reads ----------------------------------------------------------------------


def _metrics_row(eid, ep, policy, run, total, **extra):
    row = {
        "experiment_id": eid,
        "episode": ep,
        "policy": policy,
        "horizon": 1000,
        "total_reward": total,
        "curve": {"checkpoints": [500, 1000], "cum_avg_reward": [0.1, 0.1],
                  "cum_regret": [1, 2], "pct_optimal": [0.5, 0.6]},
        "per_segment": {},
        "arm_stats": {},
        **extra,
    }  # fmt: skip
    if run is not None:
        row["traffic_run"] = run
    return row


def test_metrics_and_creatives_filter_by_run_incl_legacy_null_rows():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create(scenario=SEG)).json()["experimentId"]
        await ex.drain()
        a0, a1 = (a["creativeId"] for a in h.store.rows[eid]["arms"])
        url = f"/experiments/{A}/{eid}/traffic"
        r1 = await h.client.post(url, json={"episodes": 1, "horizon": 1000})
        h.jobs.finish(r1.json()["execution"])
        await h.client.get(f"/experiments/{A}/{eid}")
        shifts = [{**VALID[1], "atFrac": 0.5}]
        r2 = await h.client.post(
            url, json={"episodes": 1, "horizon": 1000, "shifts": shifts}
        )
        h.store.metrics[eid] = [
            _metrics_row(eid, 0, "linear_ts", None, 10.0),  # legacy NULL = run 1
            _metrics_row(eid, 0, "linear_ts", 2, 20.0),
        ]
        h.store.add_events(
            eid,
            [
                {"policy": "linear_ts", "episode": 0, "round": r, "arm": a0,
                 "segment": "mobile_scrollers", "optimal_arm": a0, "clicked": 0,
                 "p_chosen": 0.04}
                for r in range(10)
            ]
            + [
                {"policy": "linear_ts", "episode": 0, "round": r, "arm": a1,
                 "segment": "mobile_scrollers", "optimal_arm": a0 if r < 500 else a1,
                 "clicked": 1, "p_chosen": 0.05, "traffic_run": 2}
                for r in (0, 499, 500, 999)
            ],
        )  # fmt: skip
        c = h.client
        base = f"/experiments/{A}/{eid}"
        m_latest = (await c.get(f"{base}/metrics")).json()
        m1 = (await c.get(f"{base}/metrics?run=1")).json()
        bad = await c.get(f"{base}/metrics?run=3")
        bad_str = await c.get(f"{base}/creatives?run=x")
        s1 = (await c.get(f"{base}/creatives?run=1")).json()
        s2 = (await c.get(f"{base}/creatives")).json()
        return (a0, a1), m_latest, m1, bad, bad_str, s1, s2, r2

    (a0, a1), m_latest, m1, bad, bad_str, s1, s2, r2 = run(go)
    assert r2.json()["run"] == 2
    assert m_latest["run"] == 2 and m_latest["totals"]["linear_ts"]["mean"] == 20.0
    assert m1["run"] == 1 and m1["totals"]["linear_ts"]["mean"] == 10.0
    assert bad.status_code == 400 and bad.json()["detail"]["reason"] == "invalid_run"
    assert bad_str.status_code == 400
    assert s1["run"] == 1 and s1["horizon"] == 10 and s1["regimes"] == []
    assert s2["run"] == 2 and s2["horizon"] == 1000
    first, after = s2["regimes"]
    assert (first["start"], first["end"], after["start"], after["end"]) == (
        0, 500, 500, 1000,
    )  # fmt: skip
    assert first["segmentWinners"] == {"mobile_scrollers": a0}
    assert after["segmentWinners"] == {"mobile_scrollers": a1}
    top = {c["creativeId"]: c for c in after["creatives"]}
    assert top[a1]["impressions"] == 2 and top[a1]["share"] == 1.0
    assert top[a1]["segmentsWon"] == ["mobile_scrollers"]
    assert top[a1]["segments"][0]["isBest"] is True
    assert top[a0]["impressions"] == 0 and top[a0]["ctr"] is None


def test_creatives_cache_is_keyed_by_run():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        a0 = h.store.rows[eid]["arms"][0]["creativeId"]
        url = f"/experiments/{A}/{eid}/traffic"
        r1 = await h.client.post(url, json={"episodes": 1, "horizon": 1000})
        h.jobs.finish(r1.json()["execution"])
        await h.client.get(f"/experiments/{A}/{eid}")
        await h.client.post(url, json={"episodes": 1, "horizon": 1000})
        ev = {"policy": "linear_ts", "episode": 0, "arm": a0, "segment": "s"}
        h.store.add_events(eid, [{**ev, "round": 0, "traffic_run": 1}])
        h.store.add_events(eid, [{**ev, "round": 3, "traffic_run": 2}])
        base = f"/experiments/{A}/{eid}/creatives"
        one = (await h.client.get(f"{base}?run=1")).json()
        two = (await h.client.get(f"{base}?run=2")).json()
        # run 1 is final once run 2 exists: cached even though status is running
        h.store.add_events(eid, [{**ev, "round": 5, "traffic_run": 1}])
        one_again = (await h.client.get(f"{base}?run=1")).json()
        return eid, one, two, one_again

    eid, one, two, one_again = run(go)
    assert one["horizon"] == 1 and two["horizon"] == 4
    assert one_again == one
    assert f"{eid}:r1" in ex._SERIES_CACHE and f"{eid}:r2" in ex._SERIES_CACHE


# --- store: SQL builders, run filter fallback --------------------------------------


def _params(params):
    return {
        p.name: (
            getattr(p, "type_", None) or p.array_type,
            getattr(p, "value", None) or p.values,
        )
        for p in params
    }


def test_run_filter_in_sql_builders():
    sql, params = build_metrics_sql("p.d.m", "e1", run=2)
    assert "IFNULL(traffic_run, 1) = @traffic_run" in sql
    assert _params(params)["traffic_run"] == ("INT64", 2)
    sql, params = build_metrics_sql("p.d.m", "e1")
    assert "traffic_run" not in sql and len(params) == 1
    sql, params = build_creative_series_sql("p.d.ev", "e1", run=1)
    assert "AND policy = @policy AND IFNULL(traffic_run, 1) = @traffic_run" in sql
    assert _params(params)["traffic_run"] == ("INT64", 1)


def test_regimes_sql_builder():
    sql, params = build_regimes_sql("p.d.ev", "e1", [20_000, 0, 12_000, 20_000], run=3)
    assert "RANGE_BUCKET(round, @boundaries) AS regime" in sql
    assert "GROUP BY regime, segment, optimal_arm, arm" in sql
    assert "IFNULL(traffic_run, 1) = @traffic_run" in sql
    assert "e1" not in sql and "12000" not in sql
    got = _params(params)
    assert got["boundaries"] == ("INT64", [12_000, 20_000])  # sorted, unique, > 0
    assert got["traffic_run"] == ("INT64", 3)
    assert got["policy"] == ("STRING", "linear_ts")
    with pytest.raises(ValueError):
        build_regimes_sql("p.d.ev", "e1", [0])


def _unmigrated_bq(missing: str) -> FakeBigQueryClient:
    """Fails queries mentioning a missing column, like an unmigrated table."""

    def results(sql, job_config):
        from google.api_core import exceptions as gexc

        if missing in sql:
            raise gexc.BadRequest(f"Unrecognized name: {missing} at [1:2]")
        return []

    return FakeBigQueryClient(results, num_dml_affected_rows=0)


def test_bigquery_store_tolerates_unmigrated_traffic_columns(caplog):
    fake = _unmigrated_bq("traffic_run")
    store = BigQueryExperimentStore(
        tables={"experiments": "p.d.x", "events": "p.d.ev", "metrics": "p.d.m"},
        client_factory=lambda: fake,
    )

    async def go():
        run1 = await store.metrics_rows("e1", run=1)
        n1 = len(fake.queries)
        run2 = await store.metrics_rows("e1", run=2)
        n2 = len(fake.queries) - n1
        series = await store.creative_series_rows("e1", run=1, boundaries=[500])
        return run1, n1, run2, n2, series

    run1, n1, run2, n2, series = asyncio.run(go())
    assert run1 == [] and n1 == 2 and "traffic_run" not in fake.sqls[1]
    assert run2 == [] and n2 == 1  # run > 1 can't exist before the migration
    assert series["regimes"] == []
    assert "every row is run 1" in caplog.text


def test_bigquery_upsert_drops_traffic_runs_on_unmigrated_table(caplog):
    fake = _unmigrated_bq("traffic_runs")
    store = BigQueryExperimentStore(
        tables={"experiments": "p.d.x", "events": "p.d.ev", "metrics": "p.d.m"},
        client_factory=lambda: fake,
    )
    row = {
        "experiment_id": "e1",
        "status": "running_traffic",
        "traffic_runs": [{"run": 1}],
    }
    asyncio.run(store.upsert(row, fields=["status", "traffic_runs"]))
    assert len(fake.queries) == 2 and "traffic_runs" not in fake.sqls[1]
    assert "migration" in caplog.text
    fake_other = _unmigrated_bq("status")
    other = BigQueryExperimentStore(
        tables={"experiments": "p.d.x", "events": "p.d.ev", "metrics": "p.d.m"},
        client_factory=lambda: fake_other,
    )
    from google.api_core import exceptions as gexc

    with pytest.raises(gexc.BadRequest):
        asyncio.run(other.upsert(row, fields=["status"]))


def test_in_memory_rows_from_events_regimes_and_run_filter():
    events = [
        {"policy": "linear_ts", "episode": 0, "round": r, "arm": "a", "segment": "s",
         "optimal_arm": "a", "clicked": 1, "p_chosen": 0.1, "traffic_run": 2}
        for r in (0, 99, 100, 150)
    ] + [{"policy": "linear_ts", "episode": 0, "round": 7, "arm": "b", "segment": "s",
          "optimal_arm": "a"}]  # fmt: skip
    legacy = series_rows_from_events(events, run=1)
    assert {r["arm"] for r in legacy["series"]} == {"b"}
    got = series_rows_from_events(events, run=2, boundaries=[150, 100])
    by_regime = {r["regime"]: r["impressions"] for r in got["regimes"]}
    assert by_regime == {0: 2, 1: 1, 2: 1}  # RANGE_BUCKET: [0,100) [100,150) [150,..)
    assert series_rows_from_events(events, run=2)["regimes"] == []
    store = InMemoryExperimentStore()
    store.metrics["e"] = [
        {"episode": 0, "policy": "x"},
        {"episode": 0, "traffic_run": 2},
    ]
    assert len(asyncio.run(store.metrics_rows("e", run=1))) == 1
    assert len(asyncio.run(store.metrics_rows("e"))) == 2


def test_build_creative_series_regimes_shape():
    rows = [{"arm": "a", "episode": 0, "win": 0, "impressions": 4, "clicks": 1,
             "horizon": 1000, "n_windows": 1}]  # fmt: skip
    regime_rows = [
        {"regime": 0, "segment": "s1", "optimal_arm": "a", "arm": "a",
         "impressions": 3, "clicks": 1, "p_sum": 0.3, "p_n": 3},
        {"regime": 1, "segment": "s1", "optimal_arm": "b", "arm": "a",
         "impressions": 1, "clicks": 0, "p_sum": 0.05, "p_n": 1},
        {"regime": 1, "segment": "s2", "optimal_arm": "b", "arm": "b",
         "impressions": 3, "clicks": 3, "p_sum": None, "p_n": 0},
    ]  # fmt: skip
    body = build_creative_series(
        rows, [], [], ["a", "b"], regime_rows=regime_rows, boundaries=[400],
        regime_horizon=1000,
    )  # fmt: skip
    first, second = body["regimes"]
    assert set(first) == {
        "index", "start", "end", "impressions", "segmentWinners", "creatives",
    }  # fmt: skip
    assert (first["index"], first["start"], first["end"]) == (0, 0, 400)
    assert (second["start"], second["end"], second["impressions"]) == (400, 1000, 4)
    assert first["segmentWinners"] == {"s1": "a"}
    assert second["segmentWinners"] == {"s1": "b", "s2": "b"}
    a, b = second["creatives"]
    assert set(a) == {
        "creativeId", "impressions", "clicks", "share", "ctr", "trueCtr",
        "segmentsWon", "segments",
    }  # fmt: skip
    assert (a["impressions"], a["share"], a["ctr"], a["trueCtr"]) == (
        1,
        0.25,
        0.0,
        0.05,
    )
    assert (b["share"], b["ctr"], b["trueCtr"]) == (0.75, 1.0, None)
    assert b["segmentsWon"] == ["s1", "s2"]
    assert [s["segment"] for s in a["segments"]] == ["s1", "s2"]  # same list for all
    assert a["segments"][1] == {
        "segment": "s2", "impressions": 0, "clicks": 0, "ctr": None,
        "trueCtr": None, "isBest": False,
    }  # fmt: skip
    assert first["creatives"][1]["impressions"] == 0  # every creative in every regime
    no_shift = build_creative_series(rows, [], [], ["a"])
    assert no_shift["regimes"] == []
    empty = build_creative_series(
        [], [], [], ["a"], boundaries=[400], regime_horizon=1000
    )
    assert [len(g["creatives"]) for g in empty["regimes"]] == [1, 1]


# --- metrics: shift_response + regimes ----------------------------------------------


def test_ghost_policy_orders_after_the_endpoint():
    assert order_policies(["oracle", "ucb1", GHOST_POLICY, "linear_ts"]) == [
        "linear_ts", GHOST_POLICY, "ucb1", "oracle",
    ]  # fmt: skip


def _sr(round_, before, after, rec):
    return {
        "round": round_,
        "pct_optimal_before": before,
        "pct_optimal_after": after,
        "regret_rate_before": 0.001,
        "regret_rate_after": 0.004,
        "recovery_rounds": rec,
    }


def test_shift_response_mean_and_ci_per_policy_incl_ghost():
    rows = [
        _metrics_row("e", ep, pol, 1, 10.0,
                     shift_response=json.dumps([_sr(500, b, a, rec)]))
        for ep, (b, a, rec) in enumerate([(0.8, 0.2, 300), (0.6, 0.4, None), (0.7, 0.3, 500)])
        for pol in ("linear_ts", GHOST_POLICY)
    ] + [_metrics_row("e", 0, "uniform", 1, 5.0)]  # fmt: skip
    m = aggregate_episode_metrics(rows, "e")
    assert set(m["shiftResponse"]) == {"linear_ts", GHOST_POLICY}
    assert m["policies"][:2] == ["linear_ts", GHOST_POLICY]
    assert GHOST_POLICY in m["curves"]
    (entry,) = m["shiftResponse"]["linear_ts"]
    assert entry["round"] == 500 and entry["episodes"] == 3
    before = entry["pctOptimalBefore"]
    assert before["mean"] == pytest.approx(0.7)
    half = 4.303 * 0.1 / math.sqrt(3)  # t(df=2) * sample std / sqrt(n)
    assert before["lo"] == pytest.approx(0.7 - half)
    assert before["hi"] == pytest.approx(min(1.0, 0.7 + half))
    assert entry["regretRateAfter"] == {"mean": 0.004, "lo": 0.004, "hi": 0.004}
    assert entry["recoveryRounds"]["mean"] == pytest.approx(400)
    assert entry["recoveryRounds"]["lo"] >= 0.0  # clamped
    assert entry["recoveredEpisodes"] == 2
    never = aggregate_episode_metrics(
        [_metrics_row("e", 0, "linear_ts", 1, 1.0,
                      shift_response=[_sr(10, 0.5, 0.5, None)])],
        "e",
    )["shiftResponse"]["linear_ts"][0]  # fmt: skip
    assert never["recoveryRounds"] is None and never["recoveredEpisodes"] == 0
    assert aggregate_episode_metrics(rows[-1:], "e")["shiftResponse"] == {}


def test_shift_response_matches_bandit_summary_on_simulated_rows():
    """The api's means equal ``bandit.metrics.summarize_shift_response`` on rows the
    simulator writes (so the aggregation consumes the exact PR A shape)."""
    from bandit import metrics, simulate
    from bandit.config import build_sim_config, shifts_from_dict

    cfg = build_sim_config(SEG, horizon=4_000, episodes=3)
    snake = [{"kind": "demote", "at_frac": 0.5, "segment": None,
              "creative_id": "leader", "drop_pp": 0.015}]  # fmt: skip
    res = simulate.run_experiment(
        cfg, ["ucb1", "uniform"], shifts=shifts_from_dict(snake), log_propensity=False
    )
    sim_rows = metrics.experiment_rows(res, spacing="linear", shift_rounds=[2_000])
    rows = json.loads(json.dumps(sim_rows, default=float))
    m = aggregate_episode_metrics(rows, cfg.experiment_id)
    want = metrics.summarize_shift_response(sim_rows)
    for policy, entries in want.items():
        (got,) = m["shiftResponse"][policy]
        (exp,) = entries
        assert got["round"] == exp["round"] == 2_000
        assert got["episodes"] == exp["episodes"]
        assert got["pctOptimalBefore"]["mean"] == pytest.approx(
            exp["pct_optimal_before"], abs=1e-5
        )
        assert got["regretRateAfter"]["mean"] == pytest.approx(
            exp["regret_rate_after"], abs=1e-5
        )
        assert got["recoveredEpisodes"] == exp["recovered_episodes"]
        if exp["recovery_rounds"] is None:
            assert got["recoveryRounds"] is None
        else:
            assert got["recoveryRounds"]["mean"] == pytest.approx(
                exp["recovery_rounds"], abs=1e-5
            )


def _regime_arrays(T, cut, opt_before, opt_after, chosen):
    return {
        "segment": np.tile([0, 1], T // 2),
        "arm": np.full(T, chosen),
        "opt_arm": np.where(np.arange(T) < cut, opt_before, opt_after),
        "reward": np.ones(T),
        "p_all": np.where(
            (np.arange(T) < cut)[:, None], [[0.06, 0.04]], [[0.03, 0.05]]
        ).astype(float),
    }


def test_regimes_aggregate_bandit_regime_stats_rows():
    """``regimes`` consumes ``bandit.metrics.regime_stats`` output (PR A shape) as the
    traffic job writes it on every row; the ghost never votes on optimalArm."""
    from bandit import metrics

    T, cut = 1_000, 400
    ids, segs = ("a", "b"), ("s0", "s1")

    def regimes(chosen, opt_after=1):
        return metrics.regime_stats(
            _regime_arrays(T, cut, 0, opt_after, chosen), [cut], ids, segs
        )

    rows = [
        _metrics_row("e", ep, "linear_ts", 1, 1.0, regimes=json.dumps(regimes(1)))
        for ep in (0, 1)
    ] + [
        # the ghost lives in the unshifted world: its "optimal" stays "a"
        _metrics_row("e", ep, GHOST_POLICY, 1, 1.0, regimes=regimes(0, opt_after=0))
        for ep in (0, 1, 2)
    ]
    m = aggregate_episode_metrics(rows, "e", arm_order=["b", "a"])
    before, after = m["regimes"]
    assert set(before) == {"start", "end", "perSegment", "arms"}
    assert (before["start"], before["end"], after["start"], after["end"]) == (
        0, cut, cut, T,
    )  # fmt: skip
    assert before["perSegment"]["s0"]["optimalArm"] == "a"
    assert after["perSegment"]["s0"]["optimalArm"] == "b"  # ghost outvoted 3:2 ignored
    lin = after["perSegment"]["s1"]["policies"]["linear_ts"]
    assert lin["pctOptimal"] == 1.0
    assert after["perSegment"]["s1"]["policies"][GHOST_POLICY]["pctOptimal"] == 1.0
    assert before["arms"] == [
        {"creativeId": "b", "trueCtr": pytest.approx(0.04)},
        {"creativeId": "a", "trueCtr": pytest.approx(0.06)},
    ]
    assert after["arms"][0]["trueCtr"] == pytest.approx(0.05)
    # whole-run fields keep their meaning (these rows' per_segment is empty)
    assert m["perSegment"] == {}
    plain = [_metrics_row("e", 0, "linear_ts", 1, 1.0)]
    assert aggregate_episode_metrics(plain, "e")["regimes"] == []


def test_env_overrides_carry_run_forget_and_snake_shifts():
    from bandit.config import shifts_from_dict, shifts_to_dict
    from runserver.experiments_jobs import build_env_overrides

    snake = ex.validate_shifts(SEG, "demo", ARMS, [VALID[2]])
    env = build_env_overrides(
        experiment_id="e1",
        config_uri="gs://b/e1/experiment.json",
        endpoint_id="projects/p/locations/r/endpoints/1",
        episodes=2,
        horizon=None,
        traffic_run=3,
        forget=True,
        shifts=snake,
    )
    got = {e["name"]: e["value"] for e in env}
    assert got["TRAFFIC_RUN"] == "3" and got["FORGET"] == "true"
    assert json.loads(got["SHIFTS_JSON"]) == snake
    # the job's strict parser accepts it unchanged
    assert shifts_to_dict(shifts_from_dict(json.loads(got["SHIFTS_JSON"]))) == snake
    plain = build_env_overrides(
        experiment_id="e1",
        config_uri="c",
        endpoint_id="x",
        episodes=1,
        horizon=1000,
        traffic_run=1,
        forget=False,
        shifts=[],
    )
    names = {e["name"]: e["value"] for e in plain}
    assert names["FORGET"] == "false" and "SHIFTS_JSON" not in names


# --- shiftCost: paired ghost - endpoint totals ----------------------------------------


def test_shift_cost_pairs_ghost_and_endpoint_by_episode():
    # (episode, endpoint reward, endpoint clicks, ghost reward, ghost clicks)
    data = [
        (0, 100.0, 100, 110.0, 112),
        (1, 50.0, 50, 62.0, 60),
        (2, 80.0, 80, 89.0, 88),
    ]
    rows = []
    for ep, r_lin, c_lin, r_gh, c_gh in data:
        rows.append(_metrics_row("e", ep, "linear_ts", 1, r_lin, total_clicks=c_lin))
        rows.append(_metrics_row("e", ep, GHOST_POLICY, 1, r_gh, total_clicks=c_gh))
    # unpaired rows are ignored: ghost episode 5 has no endpoint row and vice versa
    rows.append(_metrics_row("e", 5, GHOST_POLICY, 1, 999.0, total_clicks=999))
    rows.append(_metrics_row("e", 6, "linear_ts", 1, 1.0, total_clicks=1))
    rows.append(_metrics_row("e", 0, "uniform", 1, 1.0, total_clicks=1))
    cost = aggregate_episode_metrics(rows, "e")["shiftCost"]
    assert set(cost) == {"episodes", "clicksPerEpisode", "rewardPerEpisode"}
    assert cost["episodes"] == 3
    diffs = [10.0, 12.0, 9.0]  # paired reward differences
    mean = sum(diffs) / 3
    sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / 2)
    half = 4.303 * sd / math.sqrt(3)
    reward = cost["rewardPerEpisode"]
    assert reward["mean"] == pytest.approx(mean)
    assert reward["lo"] == pytest.approx(mean - half)
    assert reward["hi"] == pytest.approx(mean + half)
    clicks = cost["clicksPerEpisode"]
    assert clicks["mean"] == pytest.approx((12 + 10 + 8) / 3)
    # pairing is much tighter than the unpaired spread of the two totals
    assert reward["hi"] - reward["lo"] < 10.0
    # a negative cost (the shift helped) is not clamped
    flipped = [
        {**r, "total_reward": -r["total_reward"]}
        for r in rows
        if r["policy"] in ("linear_ts", GHOST_POLICY)
    ]
    assert aggregate_episode_metrics(flipped, "e")["shiftCost"]["rewardPerEpisode"][
        "mean"
    ] == pytest.approx(-mean)


def test_shift_cost_omitted_without_ghost_pairs():
    lin = [
        _metrics_row("e", ep, "linear_ts", 1, 10.0, total_clicks=10) for ep in (0, 1)
    ]
    assert "shiftCost" not in aggregate_episode_metrics(lin, "e")
    ghost_other_eps = [_metrics_row("e", 7, GHOST_POLICY, 1, 9.0, total_clicks=9)]
    assert "shiftCost" not in aggregate_episode_metrics(lin + ghost_other_eps, "e")
    assert "shiftCost" not in aggregate_episode_metrics([], "e")
    # one pair: the interval collapses to the mean
    one = aggregate_episode_metrics(
        [lin[0], _metrics_row("e", 0, GHOST_POLICY, 1, 12.5, total_clicks=13)], "e"
    )["shiftCost"]
    assert one["episodes"] == 1
    assert one["rewardPerEpisode"] == {"mean": 2.5, "lo": 2.5, "hi": 2.5}
    assert one["clicksPerEpisode"] == {"mean": 3.0, "lo": 3.0, "hi": 3.0}
    no_clicks = aggregate_episode_metrics(
        [_metrics_row("e", 0, n, 1, 1.0) for n in ("linear_ts", GHOST_POLICY)], "e"
    )["shiftCost"]
    assert no_clicks["episodes"] == 1 and no_clicks["clicksPerEpisode"] is None


def test_shift_cost_on_the_metrics_route():
    h = Harness()

    async def go():
        await h.session()
        eid = (await h.create()).json()["experimentId"]
        await ex.drain()
        h.store.metrics[eid] = [
            _metrics_row(eid, 0, "linear_ts", None, 10.0, total_clicks=10),
            _metrics_row(eid, 0, GHOST_POLICY, None, 14.0, total_clicks=15),
        ]
        return (await h.client.get(f"/experiments/{A}/{eid}/metrics")).json()

    body = run(go)
    assert body["shiftCost"]["episodes"] == 1
    assert body["shiftCost"]["clicksPerEpisode"]["mean"] == 5.0
