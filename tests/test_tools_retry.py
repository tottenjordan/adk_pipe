"""Infra tools must propagate exceptions (not swallow into status dicts) so ADK
2.0's RetryConfig can retry transient failures."""

import pytest
from google.api_core import exceptions as api_exceptions


class MockState(dict):
    pass


class MockToolContext:
    def __init__(self):
        self.state = MockState()
        self.state["gcs_folder"] = "f"
        self.state["agent_output_dir"] = "d"
        self.state["target_search_trends"] = {"target_search_trends": ["t1"]}
        self.state["brand"] = "b"
        self.state["target_audience"] = "a"
        self.state["target_product"] = "p"
        self.state["key_selling_points"] = "k"


class _BoomBQClient:
    """Fake BigQuery client whose query() raises a transient error inside the
    tool's try block (the real failure point for write_trends_to_bq)."""

    def query(self, *a, **k):
        raise api_exceptions.ServiceUnavailable("503")


class TestTrendTrawlerToolsPropagate:
    def test_get_daily_gtrends_raises_on_transient(self, monkeypatch):
        from trend_trawler import tools

        # _get_gtrends_max_date runs before the try; stub it so we reach the
        # in-try client acquisition, where the transient must propagate.
        monkeypatch.setattr(tools, "_get_gtrends_max_date", lambda: "07/01/2026")

        def boom():
            raise api_exceptions.InternalServerError("500")

        monkeypatch.setattr(tools, "_get_bigquery_client", boom)

        with pytest.raises(api_exceptions.InternalServerError):
            tools.get_daily_gtrends(MockToolContext())

    def test_write_trends_to_bq_raises_on_transient(self, monkeypatch):
        from trend_trawler import tools

        # Stub the pre-try max-date lookup (it also uses the bq client) so the
        # transient surfaces from the in-try bq_client.query() call.
        monkeypatch.setattr(tools, "_get_gtrends_max_date", lambda: "07/01/2026")
        monkeypatch.setattr(tools, "_get_bigquery_client", lambda: _BoomBQClient())

        with pytest.raises(api_exceptions.ServiceUnavailable):
            tools.write_trends_to_bq(MockToolContext())
