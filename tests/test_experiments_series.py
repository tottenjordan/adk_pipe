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
        "segments": [],
        "missedClicks": 0,
        "engagedSecondsPer1k": None,
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


# --- per-segment breakdown, missedClicks, engagedSecondsPer1k (§8, 2026-10-03) ---


def _seg(arm, segment, imps, clicks, p_sum, regret, dwell=0.0, p_n=None):
    return {
        "arm": arm,
        "segment": segment,
        "impressions": imps,
        "clicks": clicks,
        "p_sum": p_sum,
        "p_n": imps if p_n is None else p_n,
        "regret_sum": regret,
        "dwell_sum": dwell,
    }


# Shapes from the live experiment 0693ea62bb7144ef (20 episodes), scaled to 2.
CREATIVE_SEGMENTS = [
    _seg("a", "s1", 11475, 559, 0.0482 * 11475, 0.0, dwell=2236.0),
    _seg("a", "s2", 4816, 175, 0.0367 * 4816, 66.84, dwell=700.0),
    _seg("b", "s2", 12247, 642, 0.0509 * 12247, 0.0, dwell=2568.0),
    # "c" is a listed arm with no impressions; "s3" only ever went to "b"
    _seg("b", "s3", 10044, 494, 0.0501 * 10044, 1.5),
]


def _seg_series(reward_mode="click", seg_rows=CREATIVE_SEGMENTS):
    return build_creative_series(
        ROWS,
        SEGMENTS,
        TRUE,
        ARMS,
        experiment_id="e1",
        creative_segment_rows=seg_rows,
        reward_mode=reward_mode,
    )


def test_segments_same_sorted_list_for_every_creative_incl_zero_rows():
    by = _by_id(_seg_series())
    for c in by.values():
        assert [s["segment"] for s in c["segments"]] == ["s1", "s2", "s3"]
    zero = by["c"]["segments"][0]
    assert zero == {
        "segment": "s1",
        "impressions": 0,
        "clicks": 0,
        "ctr": None,
        "trueCtr": None,
        "isBest": False,
    }
    assert by["a"]["segments"][2]["ctr"] is None  # "a" never served s3
    assert by["c"]["missedClicks"] == 0 and by["c"]["engagedSecondsPer1k"] is None


def test_segment_ctr_true_ctr_and_is_best():
    by = _by_id(_seg_series())
    s1 = by["a"]["segments"][0]
    assert (s1["impressions"], s1["clicks"]) == (11475, 559)
    assert s1["ctr"] == round(559 / 11475, 4)
    assert s1["trueCtr"] == 0.0482
    # segmentsWon from SEGMENTS: a -> s1, s3; b -> s2
    assert [s["isBest"] for s in by["a"]["segments"]] == [True, False, True]
    assert [s["isBest"] for s in by["b"]["segments"]] == [False, True, False]
    assert by["b"]["segments"][1]["trueCtr"] == 0.0509


def test_true_ctr_ignores_null_p_chosen_count():
    rows = [_seg("a", "s1", 10, 1, 0.5, 0.0, p_n=5)]
    a = _by_id(_seg_series(seg_rows=rows))["a"]
    assert a["segments"][0]["trueCtr"] == 0.1
    rows = [_seg("a", "s1", 10, 1, None, 0.0, p_n=0)]
    assert _by_id(_seg_series(seg_rows=rows))["a"]["segments"][0]["trueCtr"] is None


def test_missed_clicks_is_mean_regret_per_episode():
    by = _by_id(_seg_series())
    assert by["a"]["missedClicks"] == 33.42  # 66.84 over 2 episodes
    assert by["b"]["missedClicks"] == 0.75


def test_engaged_seconds_per_1k_only_in_engaged_mode():
    click = _by_id(_seg_series("click"))
    assert all(c["engagedSecondsPer1k"] is None for c in click.values())
    eng = _by_id(_seg_series("engaged"))
    # per-1k over the creative's segment-query impressions
    assert eng["a"]["engagedSecondsPer1k"] == round(1000 * 2936.0 / 16291, 4)
    assert eng["b"]["engagedSecondsPer1k"] == round(1000 * 2568.0 / 22291, 4)
    assert eng["c"]["engagedSecondsPer1k"] is None  # 0 impressions


def test_new_fields_default_without_segment_rows():
    by = _by_id(_series())
    assert all(c["segments"] == [] for c in by.values())
    assert by["a"]["missedClicks"] == 0 and by["a"]["engagedSecondsPer1k"] is None


def test_empty_experiment_new_fields():
    s = build_creative_series(
        [], [], [], ARMS, creative_segment_rows=[], reward_mode="engaged"
    )
    for c in s["creatives"]:
        assert c["segments"] == []
        assert c["missedClicks"] == 0
        assert c["engagedSecondsPer1k"] is None


def test_rows_from_events_creative_segments_match_sql_semantics():
    events = [
        {
            "policy": "linear_ts",
            "episode": ep,
            "round": r,
            "arm": "a" if r % 2 else "b",
            "clicked": int(r % 4 == 1),
            "dwell_s": 30.0 if r % 4 == 1 else None,
            "segment": "s1" if r < 5 else "s2",
            "optimal_arm": "a",
            "p_chosen": 0.1 if r % 2 else 0.2,
            "regret": 0.0 if r % 2 else 0.1,
        }
        for ep in (0, 1)
        for r in range(10)
    ] + [{"policy": "uniform", "episode": 0, "round": 3, "arm": "z", "segment": "x"}]
    raw = series_rows_from_events(events)
    cells = {(r["arm"], r["segment"]): r for r in raw["creative_segments"]}
    assert set(cells) == {("a", "s1"), ("a", "s2"), ("b", "s1"), ("b", "s2")}
    a1 = cells[("a", "s1")]  # rounds 1, 3 per episode
    assert (a1["impressions"], a1["clicks"]) == (4, 2)
    assert a1["p_sum"] == pytest.approx(0.4) and a1["p_n"] == 4
    assert a1["dwell_sum"] == 60.0
    s = build_creative_series(
        raw["series"],
        raw["segments"],
        raw["true_ctr"],
        [],
        creative_segment_rows=raw["creative_segments"],
        reward_mode="engaged",
    )
    by = _by_id(s)
    assert by["b"]["missedClicks"] == pytest.approx(0.5)  # 5 rows x 0.1 per ep
    assert by["a"]["missedClicks"] == 0
    assert by["a"]["engagedSecondsPer1k"] == pytest.approx(1000 * 180 / 10)
    assert [x["segment"] for x in by["b"]["segments"]] == ["s1", "s2"]
