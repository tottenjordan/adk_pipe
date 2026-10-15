"""trend_scout's BigQuery/GCS tools run their blocking I/O off the event loop.

ADK 2.10 runs a sync ``FunctionTool`` inline on the event loop outside live mode
(``google/adk/tools/function_tool.py``), and the api runs agents in-process, so a
blocking ``query().result()`` or GCS upload in a sync tool stalls every
concurrent run and poll. The tools are ``async def`` with the blocking body in
``asyncio.to_thread``; their state writes stay on the loop. The model-facing
declarations must not change (golden: the declarations of the former sync
tools per backend variant, ``tests/fixtures/trend_scout_tool_declarations.json``).
"""

import asyncio
import datetime
import inspect
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from google.adk.tools import FunctionTool

from tests._fake_bq import FakeBigQueryClient
from tests._fakes import FakeStorageClient, FakeToolContext
from trend_scout import tools

_GOLDEN = Path(__file__).parent / "fixtures" / "trend_scout_tool_declarations.json"
_TOOLS = {
    "get_daily_gtrends": tools.get_daily_gtrends,
    "write_to_file": tools.write_to_file,
    "save_session_state_to_gcs": tools.save_session_state_to_gcs,
    "write_trends_to_bq": tools.write_trends_to_bq,
}


# ADK builds a different declaration per backend variant (Vertex AI adds a
# ``response_json_schema``), chosen from GOOGLE_GENAI_USE_ENTERPRISE (or the
# deprecated GOOGLE_GENAI_USE_VERTEXAI), so the variant is pinned per case. The
# deployed engines run on Vertex AI.
_VARIANT_FLAGS = {"vertex_ai": "true", "gemini_api": "false"}


@pytest.mark.parametrize("variant", sorted(_VARIANT_FLAGS))
@pytest.mark.parametrize("name", sorted(_TOOLS))
def test_tool_is_async_with_an_unchanged_declaration(monkeypatch, name, variant):
    monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", _VARIANT_FLAGS[variant])
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    tool = _TOOLS[name]
    assert inspect.iscoroutinefunction(tool)
    golden = json.loads(_GOLDEN.read_text())[variant][name]
    declaration = FunctionTool(tool)._get_declaration()
    assert declaration is not None
    assert declaration.model_dump(mode="json", exclude_none=True) == golden


class _ThreadRecordingBQ(FakeBigQueryClient):
    """FakeBigQueryClient that records the thread of every ``query()`` call."""

    def __init__(self, rows: list[dict[str, Any]]):
        super().__init__(results=rows)
        self.threads: list[int] = []

    def query(self, sql: str, job_config: Any = None) -> Any:
        self.threads.append(threading.get_ident())
        return super().query(sql, job_config)


def _run_on_loop(coro_factory) -> tuple[int, Any]:
    async def main() -> tuple[int, Any]:
        return threading.get_ident(), await coro_factory()

    return asyncio.run(main())


def _off_loop(threads: list[int], loop_thread: int) -> bool:
    return bool(threads) and all(t != loop_thread for t in threads)


def test_get_daily_gtrends_queries_off_the_loop(monkeypatch):
    date = datetime.date(2026, 10, 11)
    bq = _ThreadRecordingBQ(
        rows=[
            {"term": "b", "refresh_date": date, "max_date": date},
            {"term": "a", "refresh_date": date, "max_date": date},
        ]
    )
    monkeypatch.setattr(tools, "_get_bigquery_client", lambda: bq)
    ctx = FakeToolContext()

    loop_thread, out = _run_on_loop(lambda: tools.get_daily_gtrends(ctx))

    assert len(bq.threads) == 2  # max-date lookup + the top-terms query
    assert _off_loop(bq.threads, loop_thread)
    assert ctx.state["raw_gtrends"] == ["b", "a"]
    assert ctx.state["gtrends_refresh_date"] == "2026-10-11"
    assert "| 1 | b | 1 | 2026-10-11 |" in out


def test_write_trends_to_bq_merges_off_the_loop(monkeypatch):
    date = datetime.date(2026, 10, 11)
    bq = _ThreadRecordingBQ(rows=[{"max_date": date}])
    monkeypatch.setattr(tools, "_get_bigquery_client", lambda: bq)
    ctx = FakeToolContext(
        {
            "gcs_folder": "run",
            "target_search_trends": {"target_search_trends": ["t1", "t2"]},
            "brand": "PRS",
            "target_audience": "musicians",
            "target_product": "SE CE24",
            "key_selling_points": "tone",
        }
    )

    loop_thread, out = _run_on_loop(lambda: tools.write_trends_to_bq(ctx))

    assert len(bq.threads) == 3  # max-date lookup + one MERGE per trend
    assert _off_loop(bq.threads, loop_thread)
    assert out == {"status": "success", "trends": "t1, t2"}


class _ThreadRecordingGCS(FakeStorageClient):
    def __init__(self) -> None:
        super().__init__([])
        self.threads: list[int] = []

    def bucket(self, name: str) -> Any:
        self.threads.append(threading.get_ident())
        return super().bucket(name)


def test_write_to_file_uploads_off_the_loop(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    gcs = _ThreadRecordingGCS()
    monkeypatch.setattr(tools, "_get_gcs_client", lambda: gcs)
    ctx = FakeToolContext({"gcs_folder": "run"})

    loop_thread, out = _run_on_loop(lambda: tools.write_to_file("# hi", ctx))

    assert _off_loop(gcs.threads, loop_thread)
    assert gcs.contents["run/selected_trends.txt"] == b"# hi"
    assert out["status"] == "success"
    assert ctx.state["select_trends_markdown_gcs_uri"] == out["gcs_uri"]
    assert out["gcs_uri"].endswith("/run/selected_trends.txt")


def test_save_session_state_uploads_off_the_loop(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    gcs = _ThreadRecordingGCS()
    monkeypatch.setattr(tools, "_get_gcs_client", lambda: gcs)
    ctx = FakeToolContext({"gcs_folder": "run", "k": "v"})

    loop_thread, out = _run_on_loop(lambda: tools.save_session_state_to_gcs(ctx))

    assert _off_loop(gcs.threads, loop_thread)
    parsed = json.loads(gcs.contents["run/trawler_session_state.json"])
    assert parsed["k"] == "v"
    assert out["status"] == "success"
    assert out["gcs_uri"].endswith("/run/trawler_session_state.json")
