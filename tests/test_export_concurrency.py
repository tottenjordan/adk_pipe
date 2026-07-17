"""Concurrency regression tests for creative_agent's artifact-export tools.

Root cause of issue #104's N=5 artifact failures: the export tools wrote scratch
files to BARE RELATIVE PATHS in the one shared process CWD, then deleted them.
Under ADK's in-process concurrency (many ``run_async`` tasks in one event loop),
one run's cleanup (``shutil.rmtree`` / ``os.remove``) raced another run's write →
``[Errno 2] No such file or directory``.

These tests reproduce the race deterministically in-process (two invocations under
``asyncio.gather``) and assert per-run isolation + zero bare-CWD leaks — no GCP.
"""

import asyncio
import os

from creative_agent import gcs_tools, tools


class MockState(dict):
    pass


class MockToolContext:
    """Mirror of the double in tests/test_tools_retry.py, with the extra state
    keys the export tools read. ``gcs_folder`` is parameterized so two contexts
    represent two distinct concurrent runs."""

    def __init__(self, gcs_folder: str):
        self.state = MockState()
        self.state["gcs_folder"] = gcs_folder
        self.state["agent_output_dir"] = "creative_output"
        self.state["final_report_with_citations"] = f"# Report {gcs_folder}"
        self.state["final_visual_concepts"] = {"visual_concepts": []}
        self.state["ad_copy_critique"] = {"ad_copies": []}
        self.state["brand"] = "b"
        self.state["target_audience"] = "a"
        self.state["target_product"] = "p"
        self.state["key_selling_points"] = "k"
        self.state["target_search_trends"] = {"target_search_trends": ["t1"]}

    async def save_artifact(self, *a, **k):
        return None


class _FakeSection:
    def __init__(self, *a, **k):
        pass


class _FakeMarkdownPdf:
    """Stand-in for markdown_pdf.MarkdownPdf: .save writes a trivial file so the
    tool's subsequent open()/read() works without invoking the real renderer."""

    def __init__(self, *a, **k):
        self.meta: dict = {}

    def add_section(self, *a, **k):
        return None

    def save(self, path):
        with open(path, "wb") as f:
            f.write(b"%PDF-1.4 fake")


def test_save_draft_report_artifact_isolates_concurrent_runs(monkeypatch, tmp_path):
    """Two concurrent draft-report exports must use DISTINCT scratch paths and
    leave no bare ``report_creatives`` directory in the CWD."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gcs_tools, "MarkdownPdf", _FakeMarkdownPdf)
    monkeypatch.setattr(gcs_tools, "Section", _FakeSection)

    recorded: list[str] = []

    def _fake_upload(source_file_name, destination_blob_name):
        # The scratch file must still exist on disk at upload time.
        assert os.path.exists(source_file_name), source_file_name
        recorded.append(source_file_name)
        return "ok"

    monkeypatch.setattr(gcs_tools, "_upload_blob_to_gcs", _fake_upload)

    ctx_a = MockToolContext("run_a")
    ctx_b = MockToolContext("run_b")

    async def _both():
        return await asyncio.gather(
            gcs_tools.save_draft_report_artifact(ctx_a),
            gcs_tools.save_draft_report_artifact(ctx_b),
        )

    results = asyncio.run(_both())

    assert all(r["status"] == "success" for r in results)
    assert len(recorded) == 2
    assert recorded[0] != recorded[1]  # per-run isolation (distinct scratch paths)
    assert not os.path.exists("report_creatives")  # no bare CWD artifact leak
