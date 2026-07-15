"""Offline tests for the Cloud Logging 429/503 filter builder (no network)."""

from experiments.creative_latency.run_trial import _build_log_filter


class TestBuildLogFilter:
    def _f(self):
        return _build_log_filter(
            revision="trend-trawler-api-00040-abc",
            start_epoch=1_784_000_000.0,
            end_epoch=1_784_000_600.0,
        )

    def test_scopes_to_revision(self):
        assert (
            'resource.labels.revision_name="trend-trawler-api-00040-abc"' in self._f()
        )

    def test_scopes_to_cloud_run_revision_resource(self):
        assert 'resource.type="cloud_run_revision"' in self._f()

    def test_matches_quota_signals(self):
        f = self._f()
        # Both the HTTP-level status and the in-app model error text are covered.
        assert "429" in f
        assert "503" in f
        assert "RESOURCE_EXHAUSTED" in f

    def test_bounds_the_time_window(self):
        f = self._f()
        assert "timestamp>=" in f
        assert "timestamp<=" in f

    def test_empty_revision_returns_empty_filter(self):
        # No revision -> can't scope safely; caller treats "" as skip.
        assert _build_log_filter(revision="", start_epoch=1.0, end_epoch=2.0) == ""
