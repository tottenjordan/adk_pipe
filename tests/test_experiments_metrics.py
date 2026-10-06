"""runserver/experiments_metrics.py: pure ExperimentMetrics aggregation."""

from __future__ import annotations

import json
import math

import pytest

from runserver.experiments_metrics import (
    aggregate_episode_metrics,
    band,
    mean_std,
    order_policies,
    t_critical,
)

CPS = [10, 100, 1000]


def _row(episode, policy, total, curve_scale=1.0, *, as_json=True, arms=True):
    curve = {
        "checkpoints": CPS,
        "cum_avg_reward": [0.01 * curve_scale, 0.02 * curve_scale, 0.03 * curve_scale],
        "cum_regret": [1.0 * curve_scale, 5.0 * curve_scale, 9.0 * curve_scale],
        "pct_optimal": [0.3, 0.6, 0.9],
    }
    arm_share = {"b": [0.5, 0.3, 0.1], "a": [0.5, 0.7, 0.9]}
    per_segment = {
        "mobile": {
            "optimal_arm": "a",
            "pct_optimal": 0.8 * curve_scale,
            "avg_reward": 0.04,
            "rounds": 500,
        },
        "desktop": {
            "optimal_arm": "b",
            "pct_optimal": 0.5,
            "avg_reward": 0.02,
            "rounds": 500,
        },
    }
    arm_stats = {
        "b": {"impressions": 100, "clicks": 2, "estimated_ctr": 0.02, "true_ctr": 0.02},
        "a": {
            "impressions": 900,
            "clicks": 45,
            "estimated_ctr": 0.05,
            "true_ctr": 0.05,
        },
    }
    enc = json.dumps if as_json else (lambda v: v)
    return {
        "experiment_id": "exp1",
        "episode": episode,
        "policy": policy,
        "horizon": 1000,
        "total_reward": total,
        "total_clicks": int(total),
        "cumulative_regret": 9.0,
        "pct_optimal": 0.9,
        "steps_to_converge": 400,
        "curve": enc(curve),
        "arm_share": enc(arm_share),
        "per_segment": enc(per_segment),
        "arm_stats": enc(arm_stats) if arms else None,
        "created_at": None,
    }


def test_empty_rows_give_zero_episodes():
    m = aggregate_episode_metrics([], "exp1")
    assert m == {
        "experimentId": "exp1",
        "episodes": 0,
        "horizon": None,
        "checkpoints": [],
        "policies": [],
        "curves": {},
        "totals": {},
        "armShare": {},
        "perSegment": {},
        "arms": [],
        "shiftResponse": {},
        "regimes": [],
    }


def test_policy_order_linear_ts_first_oracle_last():
    assert order_policies(["oracle", "uniform", "zeta", "linear_ts", "ucb1"]) == [
        "linear_ts",
        "ucb1",
        "uniform",
        "zeta",
        "oracle",
    ]


def test_stats_helpers():
    assert mean_std([]) == (0.0, 0.0)
    assert mean_std([2.0]) == (2.0, 0.0)
    m, s = mean_std([1.0, 2.0, 3.0])
    assert m == 2.0 and s == pytest.approx(1.0)
    assert t_critical(1) == 0.0
    assert t_critical(2) == pytest.approx(12.706)
    assert t_critical(5) == pytest.approx(2.776)
    assert t_critical(100) == 1.96
    b = band([[1.0, 10.0], [3.0, 20.0, 99.0]])  # truncated to the shorter series
    assert b["mean"] == [2.0, 15.0]
    half = 12.706 * math.sqrt(2.0) / math.sqrt(2)
    assert b["lo"][0] == pytest.approx(2.0 - half)
    assert b["hi"][0] == pytest.approx(2.0 + half)


def test_aggregate_shapes_and_values():
    rows = []
    for ep, scale in ((0, 1.0), (1, 2.0), (2, 3.0)):
        rows.append(_row(ep, "oracle", 60.0, as_json=False))
        rows.append(_row(ep, "linear_ts", 40.0 + ep, curve_scale=scale))
        rows.append(_row(ep, "uniform", 20.0))
    m = aggregate_episode_metrics(rows, arm_order=["a", "b"])
    assert m["experimentId"] == "exp1"
    assert m["episodes"] == 3
    assert m["horizon"] == 1000
    assert m["checkpoints"] == CPS
    assert m["policies"] == ["linear_ts", "uniform", "oracle"]

    lin = m["curves"]["linear_ts"]
    assert set(lin) == {"cumAvgReward", "cumRegret", "pctOptimal"}
    # cum_regret at checkpoint 0 across episodes: 1, 2, 3 -> mean 2, sd 1, n 3
    half = 4.303 * 1.0 / math.sqrt(3)
    assert lin["cumRegret"]["mean"][0] == pytest.approx(2.0)
    assert lin["cumRegret"]["lo"][0] == pytest.approx(
        max(0.0, 2.0 - half)
    )  # clamped ≥ 0
    assert lin["cumRegret"]["hi"][0] == pytest.approx(2.0 + half)
    # identical episodes -> zero-width band
    assert m["curves"]["oracle"]["pctOptimal"]["lo"] == [0.3, 0.6, 0.9]

    assert m["totals"]["linear_ts"]["mean"] == pytest.approx(41.0)
    assert m["totals"]["linear_ts"]["std"] == pytest.approx(1.0)
    assert m["totals"]["oracle"] == {"mean": 60.0, "std": 0.0}

    assert list(m["armShare"]) == ["a", "b"]  # linear_ts only, arm_order first
    assert m["armShare"]["a"] == pytest.approx([0.5, 0.7, 0.9])

    seg = m["perSegment"]
    assert list(seg) == ["desktop", "mobile"]
    assert seg["mobile"]["optimalArm"] == "a"
    assert seg["mobile"]["policies"]["linear_ts"]["pctOptimal"] == pytest.approx(1.6)
    assert set(seg["mobile"]["policies"]) == {"linear_ts", "uniform", "oracle"}

    assert m["arms"] == [
        {
            "creativeId": "a",
            "impressions": 2700,
            "estimatedCtr": pytest.approx(0.05),
            "trueCtr": pytest.approx(0.05),
        },
        {
            "creativeId": "b",
            "impressions": 300,
            "estimatedCtr": pytest.approx(0.02),
            "trueCtr": pytest.approx(0.02),
        },
    ]


def test_single_episode_and_duplicate_rows():
    rows = [_row(0, "linear_ts", 1.0), _row(0, "linear_ts", 5.0)]  # last wins
    m = aggregate_episode_metrics(rows, "exp1")
    assert m["episodes"] == 1
    assert m["totals"]["linear_ts"] == {"mean": 5.0, "std": 0.0}
    b = m["curves"]["linear_ts"]["cumAvgReward"]
    assert b["lo"] == b["mean"] == b["hi"]


def test_missing_json_columns_are_tolerated():
    row = _row(0, "linear_ts", 1.0, arms=False)
    row["per_segment"] = ""
    row["curve"] = "not json"
    m = aggregate_episode_metrics([row], "exp1")
    assert m["arms"] == [] and m["perSegment"] == {} and m["checkpoints"] == []
    assert m["curves"]["linear_ts"]["cumRegret"] == {"mean": [], "lo": [], "hi": []}


def test_bands_clamped_to_natural_bounds():
    from runserver.experiments_metrics import CURVE_BOUNDS, band

    # Few, very noisy episodes: the raw CI would leave [0, 1].
    out = band([[0.0, 1.0], [1.0, 0.0]], CURVE_BOUNDS["pct_optimal"])
    assert all(0.0 <= v <= 1.0 for v in out["lo"] + out["hi"])
    out = band([[0.0, 5.0], [10.0, 0.0]], CURVE_BOUNDS["cum_regret"])
    assert min(out["lo"]) == 0.0
    assert band([[0.0], [1.0]])["lo"][0] < 0  # unbounded by default


# ---------------------------------------------------------------- resolvedShifts


def _sr_entry(round_, *, creative="b", resolved=True, index=0, kind="demote"):
    entry = {
        "round": round_,
        "pct_optimal_before": 0.8,
        "pct_optimal_after": 0.4,
        "regret_rate_before": 0.001,
        "regret_rate_after": 0.01,
        "recovery_rounds": 500,
    }
    if resolved:
        entry.update(
            index=index,
            kind=kind,
            end_round=None,
            segment=None,
            creative_id=creative,
            requested_creative_id="leader",
            targets=[
                {"segment": "mobile", "ctr_before": 0.05, "ctr_after": 0.03},
                {"segment": "desktop", "ctr_before": 0.04, "ctr_after": 0.02},
            ],
        )
    return entry


def _shift_row(episode, policy, entries):
    row = _row(episode, policy, 50.0)
    row["shift_response"] = json.dumps(entries)
    return row


_TARGETS_CAMEL = [
    {"segment": "mobile", "ctrBefore": 0.05, "ctrAfter": 0.03},
    {"segment": "desktop", "ctrBefore": 0.04, "ctrAfter": 0.02},
]


def test_resolved_shifts_from_the_shift_response_entries():
    shock = _sr_entry(800, creative="a", index=0, kind="shock")
    shock["end_round"] = 900
    shock["requested_creative_id"] = "a"
    mix = {
        **_sr_entry(700, resolved=False),
        "index": 1,
        "kind": "mix",
        "end_round": None,
        "segment": None,
        "creative_id": None,
        "requested_creative_id": None,
        "targets": [],
    }
    entries = [_sr_entry(500, index=2), mix, shock]
    rows = [
        _shift_row(e, p, entries) for e in range(2) for p in ("linear_ts", "random")
    ]
    body = aggregate_episode_metrics(rows)
    assert body["resolvedShifts"] == [
        {
            "index": 2,
            "kind": "demote",
            "round": 500,
            "endRound": None,
            "segment": None,
            "creativeId": "b",
            "requestedCreativeId": "leader",
            "targets": _TARGETS_CAMEL,
        },
        {
            "index": 1,
            "kind": "mix",
            "round": 700,
            "endRound": None,
            "segment": None,
            "creativeId": None,
            "requestedCreativeId": None,
            "targets": [],
        },
        {
            "index": 0,
            "kind": "shock",
            "round": 800,
            "endRound": 900,
            "segment": None,
            "creativeId": "a",
            "requestedCreativeId": "a",
            "targets": _TARGETS_CAMEL,
        },
    ]
    # the shiftResponse numbers are unchanged by the extra fields
    assert [s["round"] for s in body["shiftResponse"]["linear_ts"]] == [500, 700, 800]


def test_resolved_shifts_omitted_for_old_runs_and_runs_without_shifts():
    old = [_shift_row(0, "linear_ts", [_sr_entry(500, resolved=False)])]
    assert "resolvedShifts" not in aggregate_episode_metrics(old)
    assert "resolvedShifts" not in aggregate_episode_metrics([_row(0, "linear_ts", 5)])


def test_resolved_shifts_mismatch_uses_the_most_common(caplog):
    rows = [
        _shift_row(0, "linear_ts", [_sr_entry(500, creative="a")]),
        _shift_row(1, "linear_ts", [_sr_entry(500, creative="b")]),
        _shift_row(2, "linear_ts", [_sr_entry(500, creative="b")]),
        _shift_row(0, "random", [_sr_entry(500, resolved=False)]),
    ]
    with caplog.at_level("WARNING"):
        body = aggregate_episode_metrics(rows, experiment_id="exp1")
    assert [s["creativeId"] for s in body["resolvedShifts"]] == ["b"]
    assert "resolved shifts differ" in caplog.text


def test_resolved_shifts_tolerate_a_malformed_entry():
    bad = _sr_entry(500)
    bad["targets"] = "nope"
    del bad["index"]
    body = aggregate_episode_metrics([_shift_row(0, "linear_ts", [bad])])
    assert body["resolvedShifts"] == [
        {
            "index": 0,
            "kind": "demote",
            "round": 500,
            "endRound": None,
            "segment": None,
            "creativeId": "b",
            "requestedCreativeId": "leader",
            "targets": [],
        }
    ]
