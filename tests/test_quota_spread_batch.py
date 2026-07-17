"""Pure-core tests for the quota-spread concurrent batch harness.

The threaded network driver (``run_batch``) is the live path and is exercised in
dry-run/live only (mirroring how ``experiments.creative_latency.run_trial`` is
untested); the pure record-shaping (``assemble_batch_records``) and cell-ordering
(``plan_cell_order``) are unit-tested here with no creds or network.
"""


def test_assemble_batch_records_shapes_rows():
    from experiments.quota_spread.run_batch import assemble_batch_records

    per_run = [
        {
            "session_id": "s1",
            "status": "done",
            "error": None,
            "count_429": 2,
            "started_at": 100.0,
            "ended_at": 400.0,
            "state": {"final_creative_eval": {"pass": True}},
            "summary": {
                "total_wall_s": 300.0,
                "phase_wall_s": {"research": 120.0, "visual": 90.0, "eval": 30.0},
                "exhaustion": [],
            },
        },
        {
            "session_id": "s2",
            "status": "done",
            "error": None,
            "count_429": None,
            "started_at": 101.0,
            "ended_at": 460.0,
            "state": {},
            "summary": {
                "total_wall_s": 359.0,
                "phase_wall_s": {"research": 200.0},
                "exhaustion": ["campaign_web_search_insights__retry_exhausted"],
            },
        },
    ]

    recs = assemble_batch_records(
        arm="global_3x",
        concurrency=5,
        batch_id="b1",
        revision="rev-1",
        per_run=per_run,
    )

    assert len(recs) == 2
    assert all(r["arm"] == "global_3x" for r in recs)
    assert all(r["concurrency"] == 5 for r in recs)
    assert all(r["batch_id"] == "b1" for r in recs)
    assert all(r["revision"] == "rev-1" for r in recs)
    # research_s is pulled from the run's phase_wall_s["research"] span.
    assert recs[0]["research_s"] == 120.0
    assert recs[1]["research_s"] == 200.0
    assert recs[0]["total_s"] == 300.0
    assert recs[0]["count_429"] == 2
    assert recs[1]["count_429"] is None
    assert recs[0]["session_id"] == "s1"
    assert recs[1]["exhaustion"] == [
        "campaign_web_search_insights__retry_exhausted"
    ]
    # the full state must ride along for the free quality harvest (Task 8)
    assert recs[0]["state"] == {"final_creative_eval": {"pass": True}}


def test_assemble_batch_records_tolerates_missing_summary():
    """A crashed run (no summary) still yields a record with None metrics."""
    from experiments.quota_spread.run_batch import assemble_batch_records

    per_run = [
        {
            "session_id": None,
            "status": "error",
            "error": "boom",
            "count_429": None,
            "started_at": 1.0,
            "ended_at": 2.0,
            "state": {},
            "summary": {},
        }
    ]
    recs = assemble_batch_records(
        arm="regional_25", concurrency=1, batch_id="b0", revision="", per_run=per_run
    )
    assert len(recs) == 1
    assert recs[0]["status"] == "error"
    assert recs[0]["research_s"] is None
    assert recs[0]["total_s"] is None


def test_plan_cell_order_no_consecutive_arm_within_load():
    """Interleaved blocked ordering: within each load block no arm runs twice in a
    row (so temporal drift hits every arm equally), and loads are blocked."""
    from experiments.quota_spread.run_doe import plan_cell_order

    arms = ["global_3x", "regional_25"]
    loads = [1, 5]
    order = plan_cell_order(arms, loads, reps=4)

    assert len(order) == len(arms) * len(loads) * 4
    assert all(len(cell) == 3 for cell in order)  # (arm, N, rep)

    for load in loads:
        arms_seq = [a for (a, n, r) in order if n == load]
        for i in range(1, len(arms_seq)):
            assert arms_seq[i] != arms_seq[i - 1], (
                f"consecutive {arms_seq[i]} within load {load}"
            )
        for arm in arms:
            assert arms_seq.count(arm) == 4  # balanced reps per arm per load

    # loads are blocked (all of one load before the next) — ascending here.
    load_seq = [n for (a, n, r) in order]
    assert load_seq == sorted(load_seq)


def test_plan_cell_order_three_arms_no_consecutive():
    """The no-consecutive-arm guarantee must hold for the 3-arm (Arm C in) case."""
    from experiments.quota_spread.run_doe import plan_cell_order

    arms = ["global_3x", "regional_25", "global_altbucket"]
    order = plan_cell_order(arms, [1], reps=3)

    arms_seq = [a for (a, n, r) in order]
    assert len(order) == 3 * 3
    for i in range(1, len(arms_seq)):
        assert arms_seq[i] != arms_seq[i - 1]


def test_plan_cell_order_is_deterministic():
    """Same inputs → identical order (no random / no wall-clock)."""
    from experiments.quota_spread.run_doe import plan_cell_order

    a = plan_cell_order(["global_3x", "regional_25"], [1, 5], reps=3)
    b = plan_cell_order(["global_3x", "regional_25"], [1, 5], reps=3)
    assert a == b
