"""runserver/experiments_series.py: pure §8 per-creative time-series aggregation."""

from __future__ import annotations

import pytest

from runserver.experiments_series import build_creative_series, segment_winners
from runserver.experiments_store import series_rows_from_events


def _row(arm, ep, win, imps, clicks, horizon=100, nw=4):
    return {
        "arm": arm,
        "episode": ep,
        "win": win,
        "impressions": imps,
        "clicks": clicks,
        "horizon": horizon,
        "n_windows": nw,
    }


ROWS = [
    # episode 0: a dominates late; window 2 has no "b" impressions at all
    _row("a", 0, 0, 10, 1),
    _row("b", 0, 0, 15, 3),
    _row("a", 0, 1, 20, 2),
    _row("b", 0, 1, 5, 0),
    _row("a", 0, 2, 25, 4),
    _row("a", 0, 3, 25, 2),
    # episode 1
    _row("a", 1, 0, 5, 0),
    _row("b", 1, 0, 20, 2),
    _row("a", 1, 1, 15, 1),
    _row("b", 1, 1, 10, 1),
    _row("a", 1, 2, 25, 3),
    _row("a", 1, 3, 20, 1),
    _row("b", 1, 3, 5, 1),
]
SEGMENTS = [
    {"segment": "s1", "optimal_arm": "a", "n": 30},
    {"segment": "s1", "optimal_arm": "b", "n": 10},
    {"segment": "s2", "optimal_arm": "b", "n": 7},
    {"segment": "s3", "optimal_arm": "b", "n": 5},
    {"segment": "s3", "optimal_arm": "a", "n": 5},
]
TRUE = [{"arm": "a", "true_ctr": 0.123456}, {"arm": "b", "true_ctr": 0.05}]
ARMS = [{"creativeId": "b"}, {"creativeId": "a"}, {"creativeId": "c"}]


def _series():
    return build_creative_series(ROWS, SEGMENTS, TRUE, ARMS, experiment_id="e1")


def _by_id(series):
    return {c["creativeId"]: c for c in series["creatives"]}


def test_shape_windows_and_episodes():
    s = _series()
    assert s["experimentId"] == "e1"
    assert s["episodes"] == 2 and s["horizon"] == 100
    assert s["windows"] == [
        {"start": 0, "end": 25},
        {"start": 25, "end": 50},
        {"start": 50, "end": 75},
        {"start": 75, "end": 100},
    ]
    for c in s["creatives"]:
        assert len(c["share"]) == len(c["ctr"]) == len(c["cumClicks"]) == 4


def test_shares_sum_to_one_per_window_and_mean_over_episodes():
    s = _series()
    for w in range(4):
        assert sum(c["share"][w] for c in s["creatives"]) == pytest.approx(1, abs=1e-3)
    a = _by_id(s)["a"]
    # window 0: ep0 10/25, ep1 5/25 -> mean 0.3
    assert a["share"][0] == pytest.approx(0.3)
    assert a["finalShare"] == a["share"][-1] == pytest.approx((1 + 0.8) / 2)


def test_ctr_null_on_zero_impressions_and_pooled():
    by = _by_id(_series())
    assert by["b"]["ctr"][2] is None
    assert by["c"]["ctr"] == [None] * 4
    assert by["b"]["ctr"][0] == pytest.approx(5 / 35, abs=1e-4)


def test_cumulative_clicks_monotonic_mean_per_episode():
    by = _by_id(_series())
    for c in by.values():
        assert c["cumClicks"] == sorted(c["cumClicks"])
    assert by["a"]["cumClicks"][-1] == pytest.approx(14 / 2)
    assert by["a"]["clicks"] == 14 and by["a"]["impressions"] == 145


def test_segment_winner_argmax_and_true_ctr():
    assert segment_winners(SEGMENTS) == {"s1": "a", "s2": "b", "s3": "a"}  # tie -> id
    by = _by_id(_series())
    assert by["a"]["segmentsWon"] == ["s1", "s3"]
    assert by["b"]["segmentsWon"] == ["s2"]
    assert by["a"]["trueCtr"] == 0.1235  # rounded to 4 decimals
    assert by["c"]["trueCtr"] is None


def test_ordered_by_final_share_desc_ties_keep_arm_order():
    s = _series()
    assert [c["creativeId"] for c in s["creatives"]] == ["a", "b", "c"]
    shares = [c["finalShare"] for c in s["creatives"]]
    assert shares == sorted(shares, reverse=True)


def test_empty_lists_arms_with_empty_arrays():
    s = build_creative_series([], [], [], ARMS, experiment_id="e1")
    assert s["episodes"] == 0 and s["horizon"] is None and s["windows"] == []
    assert [c["creativeId"] for c in s["creatives"]] == ["b", "a", "c"]
    assert s["creatives"][0] == {
        "creativeId": "b",
        "share": [],
        "ctr": [],
        "cumClicks": [],
        "impressions": 0,
        "clicks": 0,
        "trueCtr": None,
        "segmentsWon": [],
        "finalShare": 0.0,
    }


def test_rows_from_events_match_sql_windowing():
    events = [
        {
            "policy": "linear_ts",
            "episode": ep,
            "round": r,
            "arm": "a" if r % 3 else "b",
            "clicked": int(r % 5 == 0),
            "segment": "s1",
            "optimal_arm": "a",
            "p_chosen": 0.1 if r % 3 else 0.3,
        }
        for ep in (0, 1)
        for r in range(50)
    ] + [{"policy": "uniform", "episode": 0, "round": 999, "arm": "z"}]
    raw = series_rows_from_events(events, windows=20)
    # horizon 50 (uniform rows ignored); 20 windows of 2.5 rounds via integer DIV
    assert {r["horizon"] for r in raw["series"]} == {50}
    assert {r["win"] for r in raw["series"]} == set(range(20))
    s = build_creative_series(raw["series"], raw["segments"], raw["true_ctr"], [])
    assert s["windows"][1] == {"start": 3, "end": 5}  # rounds 3, 4 -> DIV(r*20, 50)=1
    assert (
        sum(s["windows"][w]["end"] - s["windows"][w]["start"] for w in range(20)) == 50
    )
    by = _by_id(s)
    assert by["a"]["impressions"] + by["b"]["impressions"] == 100
    assert by["b"]["trueCtr"] == pytest.approx(0.3)
    assert by["a"]["segmentsWon"] == ["s1"]


def test_short_horizon_uses_fewer_windows():
    events = [
        {"policy": "linear_ts", "episode": 0, "round": r, "arm": "a", "clicked": 1}
        for r in range(5)
    ]
    raw = series_rows_from_events(events, windows=20)
    s = build_creative_series(raw["series"], raw["segments"], raw["true_ctr"], [])
    assert len(s["windows"]) == 5 and s["creatives"][0]["share"] == [1.0] * 5
