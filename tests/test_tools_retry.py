"""Infra tools must propagate exceptions (not swallow into status dicts) so ADK
2.0's RetryConfig can retry transient failures."""

import pytest
from google.api_core import exceptions as api_exceptions

from tests._fakes import FakeToolContext, noop_async

# Seed state the infra tools read (gcs_folder / output dir / campaign metadata).
_STATE = {
    "gcs_folder": "f",
    "agent_output_dir": "d",
    "target_search_trends": {"target_search_trends": ["t1"]},
    "brand": "b",
    "target_audience": "a",
    "target_product": "p",
    "key_selling_points": "k",
}


def _ctx() -> FakeToolContext:
    return FakeToolContext(_STATE)


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
            tools.get_daily_gtrends(_ctx())

    def test_write_trends_to_bq_raises_on_transient(self, monkeypatch):
        from trend_scout import tools

        # Stub the pre-try max-date lookup (it also uses the bq client) so the
        # transient surfaces from the in-try bq_client.query() call.
        monkeypatch.setattr(tools, "_get_gtrends_max_date", lambda: "07/01/2026")
        monkeypatch.setattr(tools, "_get_bigquery_client", lambda: _BoomBQClient())

        with pytest.raises(api_exceptions.ServiceUnavailable):
            tools.write_trends_to_bq(_ctx())


class TestCreativeAgentToolsPropagate:
    def test_write_trends_to_bq_raises_on_transient(self, monkeypatch):
        # write_trends_to_bq + _get_bigquery_client now live in creative_agent.bq_tools.
        from creative_agent import bq_tools

        monkeypatch.setattr(bq_tools, "_get_bigquery_client", lambda: _BoomBQClient())
        with pytest.raises(api_exceptions.ServiceUnavailable):
            bq_tools.write_trends_to_bq(_ctx())

    def test_save_to_gcs_raises_on_transient(self, monkeypatch):
        # _save_to_gcs + _get_gcs_client now live in creative_agent.gcs_tools.
        from creative_agent import gcs_tools

        class _BoomBlob:
            def upload_from_string(self, *a, **k):
                raise api_exceptions.ServiceUnavailable("503")

        class _BoomBucket:
            def blob(self, *a, **k):
                return _BoomBlob()

        class _BoomGCSClient:
            def bucket(self, *a, **k):
                return _BoomBucket()

        monkeypatch.setattr(gcs_tools, "_get_gcs_client", lambda: _BoomGCSClient())
        with pytest.raises(api_exceptions.ServiceUnavailable):
            gcs_tools._save_to_gcs(_ctx(), b"x", "a.png")

    def test_generate_image_retries_then_raises_on_persistent_503(self, monkeypatch):
        """A persistent 503 exhausts the backoff retries, then propagates."""
        import asyncio

        from google.genai import errors as genai_errors

        # generate_image now lives in creative_agent.image_tools and builds its
        # genai client lazily via _get_genai_client().
        from creative_agent import image_tools

        calls = {"n": 0}

        class _BoomModels:
            def generate_content(self, *a, **k):
                calls["n"] += 1
                raise genai_errors.ServerError(503, {"error": {"message": "boom"}})

        class _BoomGenaiClient:
            models = _BoomModels()

        monkeypatch.setattr(
            image_tools, "_get_genai_client", lambda: _BoomGenaiClient()
        )
        # Don't actually sleep through the backoff in tests.
        monkeypatch.setattr(image_tools.asyncio, "sleep", noop_async)

        ctx = _ctx()
        ctx.state["final_visual_concepts"] = {
            "visual_concepts": [{"image_generation_prompt": "p", "concept_name": "c"}]
        }
        with pytest.raises(genai_errors.ServerError):
            asyncio.run(image_tools.generate_image(ctx))
        # Retried up to the configured attempt ceiling (not a single try).
        assert calls["n"] == image_tools._IMAGE_GEN_MAX_ATTEMPTS

    def test_generate_image_does_not_retry_non_transient(self, monkeypatch):
        """A non-transient error (e.g. 400) propagates immediately, no retries."""
        import asyncio

        from google.genai import errors as genai_errors

        from creative_agent import image_tools

        calls = {"n": 0}

        class _BoomModels:
            def generate_content(self, *a, **k):
                calls["n"] += 1
                raise genai_errors.ClientError(400, {"error": {"message": "bad"}})

        class _BoomGenaiClient:
            models = _BoomModels()

        monkeypatch.setattr(
            image_tools, "_get_genai_client", lambda: _BoomGenaiClient()
        )
        monkeypatch.setattr(image_tools.asyncio, "sleep", noop_async)

        ctx = _ctx()
        ctx.state["final_visual_concepts"] = {
            "visual_concepts": [{"image_generation_prompt": "p", "concept_name": "c"}]
        }
        with pytest.raises(genai_errors.ClientError):
            asyncio.run(image_tools.generate_image(ctx))
        assert calls["n"] == 1

    def test_generate_image_retries_then_succeeds(self, monkeypatch):
        """Two transient 503s then a good response → the image is saved (no failure)."""
        import asyncio

        from google.genai import errors as genai_errors

        from creative_agent import image_tools

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

        monkeypatch.setattr(
            image_tools, "_get_genai_client", lambda: _FlakyGenaiClient()
        )
        monkeypatch.setattr(image_tools.asyncio, "sleep", noop_async)
        # Isolate from real GCS + artifact I/O.
        monkeypatch.setattr(image_tools, "_save_to_gcs", lambda *a, **k: "gs://b/c.png")

        ctx = _ctx()
        ctx.state["final_visual_concepts"] = {
            "visual_concepts": [{"image_generation_prompt": "p", "concept_name": "c"}]
        }

        result = asyncio.run(image_tools.generate_image(ctx))
        assert calls["n"] == 3  # 2 failures + 1 success
        assert result["status"] == "success"
