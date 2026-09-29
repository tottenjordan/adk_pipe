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
from concurrent.futures import ThreadPoolExecutor

from creative_agent import gcs_tools, tools
from tests._fakes import FakeStorageClient, FakeToolContext


def _ctx(gcs_folder: str) -> FakeToolContext:
    """The extra state keys the export tools read. ``gcs_folder`` is
    parameterized so two contexts represent two distinct concurrent runs."""
    return FakeToolContext(
        {
            "gcs_folder": gcs_folder,
            "agent_output_dir": "creative_output",
            "final_report_with_citations": f"# Report {gcs_folder}",
            "final_visual_concepts": {"visual_concepts": []},
            "ad_copy_critique": {"ad_copies": []},
            "brand": "b",
            "target_audience": "a",
            "target_product": "p",
            "key_selling_points": "k",
            "target_search_trends": {"target_search_trends": ["t1"]},
        }
    )


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

    ctx_a = _ctx("run_a")
    ctx_b = _ctx("run_b")

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


def test_save_creative_gallery_html_isolates_concurrent_runs(monkeypatch, tmp_path):
    """Two concurrent gallery exports must use DISTINCT scratch paths and leave no
    bare ``creative_portfolio_gallery.html`` in the CWD."""
    monkeypatch.chdir(tmp_path)

    recorded: list[str] = []

    def _fake_upload(source_file_name, destination_blob_name):
        assert os.path.exists(source_file_name), source_file_name
        recorded.append(source_file_name)
        return "ok"

    monkeypatch.setattr(tools, "_upload_blob_to_gcs", _fake_upload)

    ctx_a = _ctx("run_a")
    ctx_b = _ctx("run_b")

    async def _both():
        return await asyncio.gather(
            tools.save_creative_gallery_html(ctx_a),
            tools.save_creative_gallery_html(ctx_b),
        )

    results = asyncio.run(_both())

    assert all(r["status"] == "success" for r in results)
    assert len(recorded) == 2
    assert recorded[0] != recorded[1]
    assert not os.path.exists("creative_portfolio_gallery.html")


class _FakeResized:
    def save(self, path):
        with open(path, "wb") as f:
            f.write(b"resized")


class _FakeImg:
    size = (10, 10)

    def resize(self, size, resample):
        return _FakeResized()

    def close(self):
        pass


class _FakeImage:
    class Resampling:
        LANCZOS = 1

    @staticmethod
    def open(path):
        assert os.path.exists(path), path
        return _FakeImg()


def test_get_high_res_img_isolates_concurrent_runs(monkeypatch, tmp_path):
    """Two concurrent resizes of the SAME artifact_key (as two runs producing a
    like-named concept would) must not collide on a shared CWD scratch file, and
    must leave no bare local_*/XL_* files behind. Also guards the gotcha: the GCS
    object name must be the file basename, not the temp-dir path."""
    monkeypatch.chdir(tmp_path)
    uploads: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gcs_tools,
        "_get_gcs_client",
        lambda: FakeStorageClient(uploads, download_bytes=b"origbytes"),
    )
    monkeypatch.setattr(gcs_tools, "Image", _FakeImage)

    artifact_key = "concept.png"  # identical key => same bare filename on old code

    def _call(folder):
        return gcs_tools._get_high_res_img(
            gcs_folder=folder, gcs_subdir="creative_output", artifact_key=artifact_key
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        uris = list(ex.map(_call, ["run_a", "run_b"]))

    assert all(u for u in uris)  # both returned a URI
    assert len(uploads) == 2
    xl_paths = [p for _, p in uploads]
    assert xl_paths[0] != xl_paths[1]  # per-run isolation
    # no bare scratch files leaked into CWD
    assert not os.path.exists("local_concept.png")
    assert not os.path.exists("XL_local_concept.png")
    # gotcha: blob name uses the basename, not the temp-dir path
    for name, _ in uploads:
        assert name.endswith("resized/XL_local_concept.png"), name
