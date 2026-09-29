"""Concurrency regression tests for trend_scout's GCS-export tools.

Same root cause as issue #104 (fixed in creative_agent, see
``tests/test_export_concurrency.py``): ``write_to_file`` and
``save_session_state_to_gcs`` wrote scratch files to a FIXED relative directory
(``trawler_output`` — ``state["agent_output_dir"]``) with FIXED filenames, then
``shutil.rmtree``'d it. Under ADK's in-process concurrency (many ``run_async``
tasks in one event loop, one shared CWD), one run's ``rmtree`` raced another
run's write → wrong-data upload or ``FileNotFoundError``.

These tests reproduce the race deterministically in-process (two invocations via
``ThreadPoolExecutor``) and assert per-run isolation + zero bare-CWD leaks — no GCP.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor

from tests._fakes import FakeStorageClient, FakeToolContext
from trend_scout import tools


def _ctx(gcs_folder: str) -> FakeToolContext:
    """A distinct concurrent run per ``gcs_folder``."""
    return FakeToolContext(
        {"gcs_folder": gcs_folder, "agent_output_dir": "trawler_output"}
    )


def test_write_to_file_isolates_concurrent_runs(monkeypatch, tmp_path):
    """Two concurrent ``write_to_file`` calls must use DISTINCT scratch paths,
    produce per-run-distinct GCS object names, and leave no bare
    ``trawler_output`` directory in the CWD."""
    monkeypatch.chdir(tmp_path)
    uploads: list[tuple[str, str]] = []
    monkeypatch.setattr(tools, "_get_gcs_client", lambda: FakeStorageClient(uploads))

    def _call(folder):
        return tools.write_to_file(f"# trends for {folder}", _ctx(folder))

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(_call, ["run_a", "run_b"]))

    assert all(r["status"] == "success" for r in results)
    assert len(uploads) == 2
    scratch_paths = [p for _, p in uploads]
    assert scratch_paths[0] != scratch_paths[1]  # per-run isolation
    object_names = [n for n, _ in uploads]
    assert sorted(object_names) == [
        "run_a/selected_trends.txt",
        "run_b/selected_trends.txt",
    ]
    assert not os.path.exists("trawler_output")  # no bare CWD artifact leak


def test_save_session_state_isolates_concurrent_runs(monkeypatch, tmp_path):
    """Two concurrent ``save_session_state_to_gcs`` calls must isolate scratch
    files and leave no bare ``trawler_output`` directory in the CWD."""
    monkeypatch.chdir(tmp_path)
    uploads: list[tuple[str, str]] = []
    monkeypatch.setattr(tools, "_get_gcs_client", lambda: FakeStorageClient(uploads))

    def _call(folder):
        return tools.save_session_state_to_gcs(_ctx(folder))

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(_call, ["run_a", "run_b"]))

    assert all(r["status"] == "success" for r in results)
    assert len(uploads) == 2
    scratch_paths = [p for _, p in uploads]
    assert scratch_paths[0] != scratch_paths[1]  # per-run isolation
    object_names = [n for n, _ in uploads]
    assert sorted(object_names) == [
        "run_a/trawler_session_state.json",
        "run_b/trawler_session_state.json",
    ]
    assert not os.path.exists("trawler_output")


def test_write_to_file_uploads_correct_content(monkeypatch, tmp_path):
    """Guard against the race's *silent* failure mode: the uploaded scratch file
    must contain THIS run's content (not another run's), so the object key uses
    the file basename and the temp dir never leaks into it."""
    monkeypatch.chdir(tmp_path)
    client = FakeStorageClient([])
    monkeypatch.setattr(tools, "_get_gcs_client", lambda: client)

    tools.write_to_file("# unique content", _ctx("solo"))
    assert client.contents["solo/selected_trends.txt"] == b"# unique content"


def test_save_session_state_uploads_valid_json(monkeypatch, tmp_path):
    """The session-state export must upload parseable JSON of this run's state."""
    monkeypatch.chdir(tmp_path)
    client = FakeStorageClient([])
    monkeypatch.setattr(tools, "_get_gcs_client", lambda: client)

    ctx = _ctx("solo")
    ctx.state["some_key"] = "some_value"
    tools.save_session_state_to_gcs(ctx)

    parsed = json.loads(client.contents["solo/trawler_session_state.json"])
    assert parsed["some_key"] == "some_value"
