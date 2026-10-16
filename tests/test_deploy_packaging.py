"""Engine packaging: what deploy_agent.py hands the AgentPlatform SDK.

The SDK tars every ``extra_packages`` entry with a bare ``tarfile.add(path)``
(no filter), so whatever sits in a package dir ships: a local ``adk web`` run's
``.adk/`` (session.db, rendered PNGs, PDFs, eval history), ``__pycache__`` and
caches. deploy_agent therefore stages a filtered copy of each package, flat
under its own folder name, and runs the SDK from the staging root: the archive
holds ``./<basename>/...`` whatever the package's repo path (``./agents/x`` or
``./libs/y``), because the SDK's tar arcname becomes the import path.
"""

import os
import tarfile

import pytest


def _da():
    try:
        import deployment.deploy_agent as deploy_agent
    except ImportError as e:  # agentplatform/absl missing in a bare env
        pytest.skip(f"deploy_agent import unavailable: {e}")
    return deploy_agent


def _write(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def fake_repo(tmp_path):
    src = tmp_path / "repo"
    pkg = src / "fake_agent"
    _write(pkg / "__init__.py", b"")
    _write(pkg / "agent.py", b"root_agent = None\n")
    _write(pkg / "sub_agents" / "planner" / "agent.py", b"x = 1\n")
    _write(pkg / "data" / "palette.yaml", b"a: 1\n")
    # local-only junk that must never ship
    _write(pkg / ".adk" / "session.db", b"sqlite")
    _write(pkg / ".adk" / "artifacts" / "u" / "img.png", b"png")
    _write(pkg / "__pycache__" / "agent.cpython-313.pyc", b"pyc")
    _write(pkg / "sub_agents" / "__pycache__" / "x.cpython-313.pyc", b"pyc")
    _write(pkg / "sub_agents" / "planner" / "stale.pyc", b"pyc")
    _write(pkg / ".pytest_cache" / "v" / "cache", b"c")
    _write(pkg / ".env", b"SECRET=1")
    _write(src / "other_pkg" / "__init__.py", b"")
    _write(src / "other_pkg" / ".adk" / "session.db", b"sqlite")
    return src


def _files(root):
    out = set()
    for dirpath, _, names in os.walk(root):
        for n in names:
            out.add(os.path.relpath(os.path.join(dirpath, n), root))
    return out


class TestStageExtraPackages:
    def test_excludes_local_data_and_keeps_sources_at_same_paths(
        self, fake_repo, tmp_path
    ):
        da = _da()
        dest = tmp_path / "staged"
        dest.mkdir()
        staged = da.stage_extra_packages(
            ["./fake_agent", "./other_pkg"], str(dest), src_root=str(fake_repo)
        )
        # Same relative names as AGENT_EXTRA_PACKAGES, resolved against dest.
        assert staged == ["./fake_agent", "./other_pkg"]
        assert _files(dest) == {
            "fake_agent/__init__.py",
            "fake_agent/agent.py",
            "fake_agent/sub_agents/planner/agent.py",
            "fake_agent/data/palette.yaml",
            "other_pkg/__init__.py",
        }
        assert (dest / "fake_agent" / "agent.py").read_bytes() == (
            b"root_agent = None\n"
        )

    def test_sdk_style_tar_from_staging_root_has_import_paths(
        self, fake_repo, tmp_path, monkeypatch
    ):
        """Mirror the SDK's ``tar.add(path)`` from the staging cwd."""
        da = _da()
        dest = tmp_path / "staged"
        dest.mkdir()
        staged = da.stage_extra_packages(
            ["./fake_agent"], str(dest), src_root=str(fake_repo)
        )
        archive = tmp_path / "deps.tar.gz"
        monkeypatch.chdir(dest)
        with tarfile.open(archive, "w:gz") as tar:
            for p in staged:
                tar.add(p)
        names = {m.name for m in tarfile.open(archive).getmembers() if m.isfile()}
        assert "./fake_agent/agent.py" in names
        assert "./fake_agent/sub_agents/planner/agent.py" in names
        assert not any(".adk" in n or "__pycache__" in n for n in names)
        assert not any(n.endswith(".pyc") or n.endswith(".env") for n in names)

    def test_real_packages_stage_without_local_data(self, tmp_path):
        da = _da()
        pkgs = da.AGENT_EXTRA_PACKAGES["interactive_creative"]
        da.stage_extra_packages(pkgs, str(tmp_path))
        files = _files(tmp_path)
        assert "creative_agent/agent.py" in files
        assert "interactive_creative/agent.py" in files
        assert not [f for f in files if "/." in f or "__pycache__" in f]
        da.assert_no_local_data(str(tmp_path))  # does not raise


@pytest.fixture
def nested_repo(tmp_path):
    """Packages under container dirs, like agents/ and libs/."""
    src = tmp_path / "nested"
    _write(src / "agents" / "x" / "__init__.py", b"")
    _write(src / "agents" / "x" / "agent.py", b"from y import z\n")
    _write(src / "agents" / "x" / ".adk" / "session.db", b"sqlite")
    _write(src / "libs" / "y" / "__init__.py", b"z = 1\n")
    _write(src / "libs" / "y" / "sub" / "mod.py", b"")
    return src


class TestFlatStaging:
    def test_nested_packages_stage_flat_under_their_basenames(
        self, nested_repo, tmp_path
    ):
        da = _da()
        dest = tmp_path / "staged"
        dest.mkdir()
        staged = da.stage_extra_packages(
            ["./agents/x", "./libs/y"], str(dest), src_root=str(nested_repo)
        )
        assert staged == ["./x", "./y"]
        assert _files(dest) == {
            "x/__init__.py",
            "x/agent.py",
            "y/__init__.py",
            "y/sub/mod.py",
        }

    def test_sdk_style_tar_top_level_names_are_import_names(
        self, nested_repo, tmp_path, monkeypatch
    ):
        da = _da()
        dest = tmp_path / "staged"
        dest.mkdir()
        staged = da.stage_extra_packages(
            ["./agents/x", "./libs/y"], str(dest), src_root=str(nested_repo)
        )
        archive = tmp_path / "deps.tar.gz"
        monkeypatch.chdir(dest)
        with tarfile.open(archive, "w:gz") as tar:
            for p in staged:
                tar.add(p)
        names = {
            m.name.removeprefix("./")
            for m in tarfile.open(archive).getmembers()
            if m.isfile()
        }
        assert names == {"x/__init__.py", "x/agent.py", "y/__init__.py", "y/sub/mod.py"}
        assert {n.split("/")[0] for n in names} == {"x", "y"}

    def test_duplicate_basenames_raise(self, tmp_path):
        da = _da()
        src = tmp_path / "dup"
        _write(src / "agents" / "x" / "__init__.py", b"")
        _write(src / "libs" / "x" / "__init__.py", b"")
        dest = tmp_path / "staged"
        dest.mkdir()
        with pytest.raises(ValueError, match="x"):
            da.stage_extra_packages(
                ["./agents/x", "./libs/x"], str(dest), src_root=str(src)
            )
        assert not list(dest.iterdir())  # nothing staged before the check


class TestAssertNoLocalData:
    @pytest.mark.parametrize(
        "bad",
        [
            "pkg/.adk/session.db",
            "pkg/sub/.adk/artifacts/a.png",
            "pkg/__pycache__/a.cpython-313.pyc",
            "pkg/stale.pyc",
        ],
    )
    def test_refuses_local_only_files(self, tmp_path, bad):
        da = _da()
        _write(tmp_path / "pkg" / "agent.py")
        _write(tmp_path / bad)
        with pytest.raises(RuntimeError, match="local-only"):
            da.assert_no_local_data(str(tmp_path))

    def test_passes_clean_tree(self, tmp_path):
        da = _da()
        _write(tmp_path / "pkg" / "agent.py")
        da.assert_no_local_data(str(tmp_path))


class TestLoadRequirements:
    def test_strips_uv_comments_annotations_and_blanks(self, tmp_path):
        da = _da()
        req = tmp_path / "requirements.txt"
        req.write_text(
            "# This file was autogenerated by uv via the following command:\n"
            "#    uv export --format requirements-txt --no-hashes --no-dev\n"
            "absl-py==2.5.0\n"
            "    # via\n"
            "    #   adk-pipe\n"
            "\n"
            "pywin32==311 ; sys_platform == 'win32'\n"
            "    # via portalocker\n"
            "google-adk==2.10.0  # inline note\n"
        )
        assert da.load_requirements(str(req)) == [
            "absl-py==2.5.0",
            "pywin32==311 ; sys_platform == 'win32'",
            "google-adk==2.10.0",
        ]

    def test_real_requirements_have_no_comment_lines(self):
        da = _da()
        reqs = da.load_requirements(os.path.join(da.project_root, "requirements.txt"))
        assert reqs
        assert not [r for r in reqs if r.startswith("#") or not r.strip()]
        assert any(r.startswith("google-adk") for r in reqs)
