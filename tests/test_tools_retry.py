"""Infra tools must propagate exceptions (not swallow into status dicts) so ADK
2.0's RetryConfig can retry transient failures."""

import pytest
from google.api_core import exceptions as api_exceptions


async def _noop_async(*a, **k):
    """Stand-in for asyncio.sleep so backoff retries don't wall-clock in tests."""
    return None


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

    async def save_artifact(self, *a, **k):
        return None


class _BoomBQClient:
    """Fake BigQuery client whose query() raises a transient error inside the
    tool's try block (the real failure point for write_trends_to_bq)."""

    def query(self, *a, **k):
        raise api_exceptions.ServiceUnavailable("503")


class TestTrendTrawlerToolsPropagate:
    def test_get_daily_gtrends_raises_on_transient(self, monkeypatch):
        from trend_scout import tools

        # _get_gtrends_max_date runs before the try; stub it so we reach the
        # in-try client acquisition, where the transient must propagate.
        monkeypatch.setattr(tools, "_get_gtrends_max_date", lambda: "07/01/2026")

        def boom():
            raise api_exceptions.InternalServerError("500")

        monkeypatch.setattr(tools, "_get_bigquery_client", boom)

        with pytest.raises(api_exceptions.InternalServerError):
            tools.get_daily_gtrends(MockToolContext())

    def test_write_trends_to_bq_raises_on_transient(self, monkeypatch):
        from trend_scout import tools

        # Stub the pre-try max-date lookup (it also uses the bq client) so the
        # transient surfaces from the in-try bq_client.query() call.
        monkeypatch.setattr(tools, "_get_gtrends_max_date", lambda: "07/01/2026")
        monkeypatch.setattr(tools, "_get_bigquery_client", lambda: _BoomBQClient())

        with pytest.raises(api_exceptions.ServiceUnavailable):
            tools.write_trends_to_bq(MockToolContext())


class TestCreativeAgentToolsPropagate:
    def test_write_trends_to_bq_raises_on_transient(self, monkeypatch):
        from creative_agent import tools

        class _BoomBQClient:
            def query(self, *a, **k):
                raise api_exceptions.ServiceUnavailable("503")

        monkeypatch.setattr(tools, "_get_bigquery_client", lambda: _BoomBQClient())
        with pytest.raises(api_exceptions.ServiceUnavailable):
            tools.write_trends_to_bq(MockToolContext())

    def test_save_to_gcs_raises_on_transient(self, monkeypatch):
        from creative_agent import tools

        class _BoomBlob:
            def upload_from_string(self, *a, **k):
                raise api_exceptions.ServiceUnavailable("503")

        class _BoomBucket:
            def blob(self, *a, **k):
                return _BoomBlob()

        class _BoomGCSClient:
            def bucket(self, *a, **k):
                return _BoomBucket()

        monkeypatch.setattr(tools, "_get_gcs_client", lambda: _BoomGCSClient())
        with pytest.raises(api_exceptions.ServiceUnavailable):
            tools._save_to_gcs(MockToolContext(), b"x", "a.png")

    def test_generate_image_retries_then_raises_on_persistent_503(self, monkeypatch):
        """A persistent 503 exhausts the backoff retries, then propagates."""
        import asyncio
        from google.genai import errors as genai_errors
        from creative_agent import tools

        calls = {"n": 0}

        class _BoomModels:
            def generate_content(self, *a, **k):
                calls["n"] += 1
                raise genai_errors.ServerError(503, {"error": {"message": "boom"}})

        class _BoomGenaiClient:
            models = _BoomModels()

        monkeypatch.setattr(tools, "client", _BoomGenaiClient())
        # Don't actually sleep through the backoff in tests.
        monkeypatch.setattr(tools.asyncio, "sleep", _noop_async)

        ctx = MockToolContext()
        ctx.state["final_visual_concepts"] = {
            "visual_concepts": [{"image_generation_prompt": "p", "concept_name": "c"}]
        }
        with pytest.raises(genai_errors.ServerError):
            asyncio.run(tools.generate_image(ctx))
        # Retried up to the configured attempt ceiling (not a single try).
        assert calls["n"] == tools._IMAGE_GEN_MAX_ATTEMPTS

    def test_generate_image_does_not_retry_non_transient(self, monkeypatch):
        """A non-transient error (e.g. 400) propagates immediately, no retries."""
        import asyncio
        from google.genai import errors as genai_errors
        from creative_agent import tools

        calls = {"n": 0}

        class _BoomModels:
            def generate_content(self, *a, **k):
                calls["n"] += 1
                raise genai_errors.ClientError(400, {"error": {"message": "bad"}})

        class _BoomGenaiClient:
            models = _BoomModels()

        monkeypatch.setattr(tools, "client", _BoomGenaiClient())
        monkeypatch.setattr(tools.asyncio, "sleep", _noop_async)

        ctx = MockToolContext()
        ctx.state["final_visual_concepts"] = {
            "visual_concepts": [{"image_generation_prompt": "p", "concept_name": "c"}]
        }
        with pytest.raises(genai_errors.ClientError):
            asyncio.run(tools.generate_image(ctx))
        assert calls["n"] == 1

    def test_generate_image_retries_then_succeeds(self, monkeypatch):
        """Two transient 503s then a good response → the image is saved (no failure)."""
        import asyncio
        from google.genai import errors as genai_errors
        from creative_agent import tools

        calls = {"n": 0}

        class _Part:
            class inline_data:
                data = b"\x89PNG"
                mime_type = "image/png"

        class _Content:
            parts = [_Part()]

        class _Candidate:
            content = _Content()

        class _GoodResponse:
            candidates = [_Candidate()]

        class _FlakyModels:
            def generate_content(self, *a, **k):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise genai_errors.ServerError(503, {"error": {"message": "boom"}})
                return _GoodResponse()

        class _FlakyGenaiClient:
            models = _FlakyModels()

        monkeypatch.setattr(tools, "client", _FlakyGenaiClient())
        monkeypatch.setattr(tools.asyncio, "sleep", _noop_async)
        # Isolate from real GCS + artifact I/O.
        monkeypatch.setattr(tools, "_save_to_gcs", lambda *a, **k: "gs://b/c.png")

        ctx = MockToolContext()
        ctx.state["final_visual_concepts"] = {
            "visual_concepts": [{"image_generation_prompt": "p", "concept_name": "c"}]
        }

        result = asyncio.run(tools.generate_image(ctx))
        assert calls["n"] == 3  # 2 failures + 1 success
        assert result["status"] == "success"
