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


def test_gallery_links_original_images_without_upscale(monkeypatch, tmp_path):
    """The gallery lightbox links the original rendered PNG: no download, no
    1.5x upscale and no ``resized/XL_local_*`` re-upload (each cost ~7 s per
    image in finalize): the GCS client is never even built."""
    monkeypatch.chdir(tmp_path)
    gcs_clients: list[FakeStorageClient] = []

    def _client():
        gcs_clients.append(FakeStorageClient([]))
        return gcs_clients[-1]

    monkeypatch.setattr(gcs_tools, "_get_gcs_client", _client)
    written: list[str] = []
    destinations: list[str] = []

    def _fake_upload(source_file_name, destination_blob_name):
        with open(source_file_name) as f:
            written.append(f.read())
        destinations.append(destination_blob_name)
        return "ok"

    monkeypatch.setattr(tools, "_upload_blob_to_gcs", _fake_upload)
    concepts = [
        {
            "concept_name": f"Concept {n}",
            "visual_style": "flat",
            "trend": "t",
            "trend_reference": "r",
            "concept_summary": "s",
            "markets_product": "m",
            "audience_appeal": "a",
            "selection_rationale": "r",
            "image_generation_prompt": "p",
            "headline": f"H{n}",
            "social_caption": "c",
        }
        for n in ("A", "B")
    ]
    ctx = _ctx("run_a")
    ctx.state["final_visual_concepts"] = {"visual_concepts": concepts}

    result = asyncio.run(tools.save_creative_gallery_html(ctx))

    assert result["status"] == "success"
    assert gcs_clients == []  # no download / resize / re-upload
    assert len(destinations) == 1  # only the gallery HTML itself
    (html,) = written
    assert "resized/" not in html and "XL_local_" not in html
    for concept in concepts:
        url = (
            "https://storage.mtls.cloud.google.com/"
            f"{gcs_tools.config.GCS_BUCKET_NAME}/run_a/creative_output/"
            f"{gcs_tools.artifact_key_for(concept['concept_name'])}?authuser=3"
        )
        assert f'data-high-res-src="{url}"' in html
        assert f'src="{url}"' in html
