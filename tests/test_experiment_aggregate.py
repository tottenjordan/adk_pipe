"""Offline tests for the N-trial aggregation math (no creds, no network)."""

from experiments.creative_latency.run_experiment import aggregate_records


def _record(*, status="done", total, phases, calls=None, http=None):
    return {
        "status": status,
        "session_id": f"s-{total}",
        "http_429_503": http,
        "summary": {
            "total_wall_s": total,
            "phase_wall_s": phases,
            "model_calls": calls or {},
        },
    }


class TestAggregateRecords:
    def _agg(self):
        records = [
            _record(total=100.0, phases={"research": 60.0, "eval": 40.0}, http=2),
            _record(total=120.0, phases={"research": 80.0, "eval": 40.0}, http=4),
            _record(total=140.0, phases={"research": 90.0, "eval": 50.0}, http=6),
        ]
        return aggregate_records("baseline", records)

    def test_counts(self):
        agg = self._agg()
        assert agg["config"] == "baseline"
        assert agg["n_trials"] == 3
        assert agg["n_done"] == 3

    def test_total_median_min_max(self):
        agg = self._agg()
        assert agg["total_wall_s"] == {"median": 120.0, "min": 100.0, "max": 140.0}

    def test_per_phase_stats(self):
        agg = self._agg()
        assert agg["phase_wall_s"]["research"] == {
            "median": 80.0,
            "min": 60.0,
            "max": 90.0,
        }
        assert agg["phase_wall_s"]["eval"]["median"] == 40.0

    def test_http_429_503_aggregated_when_present(self):
        assert self._agg()["http_429_503"] == {"median": 4, "min": 2, "max": 6}

    def test_only_done_trials_are_aggregated(self):
        records = [
            _record(total=100.0, phases={"research": 100.0}),
            _record(status="error", total=5.0, phases={"research": 5.0}),
        ]
        agg = aggregate_records("baseline", records)
        assert agg["n_trials"] == 2
        assert agg["n_done"] == 1
        # The 5s error run must NOT drag the median down.
        assert agg["total_wall_s"]["min"] == 100.0

    def test_http_none_when_no_data(self):
        records = [_record(total=100.0, phases={"research": 100.0}, http=None)]
        assert aggregate_records("baseline", records)["http_429_503"] is None

    def test_empty_records_safe(self):
        agg = aggregate_records("baseline", [])
        assert agg["n_trials"] == 0
        assert agg["n_done"] == 0
        assert agg["total_wall_s"] is None
