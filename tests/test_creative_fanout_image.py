"""CRF container image: one Dockerfile serves both Cloud Run functions.

``creative-trawler-crf`` (orchestrator) and ``creative-worker-crf`` (worker) run the
same image; ``FUNCTION_TARGET`` (set per service) picks the entry point. Named so
the ``tests/test_crf_*.py`` glob of the CRF-deps CI venv (no pyyaml) skips it.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CRF_DIR = REPO_ROOT / "cloud_functions" / "creative_fanout"
DOCKERFILE = CRF_DIR / "Dockerfile"
DOCKERIGNORE = CRF_DIR / "Dockerfile.dockerignore"


def _instructions(path: Path) -> list[tuple[str, str]]:
    """``(KEYWORD, args)`` per Dockerfile instruction (continuations joined)."""
    out, buf = [], ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        keyword, _, args = buf.partition(" ")
        out.append((keyword.upper(), args.strip()))
        buf = ""
    return out


def test_dependencies_install_once_before_code():
    ins = _instructions(DOCKERFILE)
    installs = [i for i, (k, a) in enumerate(ins) if k == "RUN" and "pip install" in a]
    assert len(installs) == 1, installs
    assert "-r /build/requirements.txt" in ins[installs[0]][1]
    copies = [(i, a) for i, (k, a) in enumerate(ins) if k == "COPY"]
    before = [a for i, a in copies if i < installs[0] and "--from" not in a]
    assert before == [
        "cloud_functions/creative_fanout/requirements.txt /build/requirements.txt"
    ]
    code = [i for i, a in copies if "--from" not in a and "requirements.txt" not in a]
    assert code and min(code) > installs[0]


def test_multi_stage_uv_venv_non_root():
    ins = _instructions(DOCKERFILE)
    froms = [a for k, a in ins if k == "FROM"]
    assert len(froms) == 3, froms  # uv, builder, final
    assert ("COPY", "--from=builder /opt/venv /opt/venv") in ins
    last_from = max(i for i, (k, _) in enumerate(ins) if k == "FROM")
    users = [a for k, a in ins[last_from:] if k == "USER"]
    assert users and users[-1] not in ("root", "0"), users


def test_no_function_target_default():
    """Each service sets FUNCTION_TARGET; a baked-in default would let a
    misconfigured service silently run the other entry point."""
    for keyword, args in _instructions(DOCKERFILE):
        if keyword in ("ENV", "ARG"):
            assert "FUNCTION_TARGET" not in args, (keyword, args)


def test_start_command_runs_functions_framework_from_env_target():
    ins = _instructions(DOCKERFILE)
    starts = [(k, a) for k, a in ins if k in ("CMD", "ENTRYPOINT")]
    assert len(starts) == 1, starts
    cmd = starts[0][1]
    # Shell form (via sh -c) so $FUNCTION_TARGET / $PORT expand at start.
    assert cmd.startswith('["/bin/sh", "-c",'), cmd
    assert "exec functions-framework" in cmd
    # Package-style load: main.py's relative imports need --source=main.py.
    assert "--source=main.py" in cmd
    # ${VAR:?msg} makes sh exit at once when the target is unset.
    assert re.search(r"--target=(\\\")?\$\{FUNCTION_TARGET:\?", cmd), cmd
    assert "--signature-type=cloudevent" in cmd
    assert "${PORT:-8080}" in cmd


def test_dockerignore_allowlists_exactly_the_function_files():
    lines = [
        ln.strip()
        for ln in DOCKERIGNORE.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert lines[0] == "**"
    allowed = {ln[1:] for ln in lines[1:]}
    assert all(ln.startswith("!") for ln in lines[1:]), lines
    assert allowed == {
        "cloud_functions/creative_fanout/__init__.py",
        "cloud_functions/creative_fanout/main.py",
        "cloud_functions/creative_fanout/config.py",
        "cloud_functions/creative_fanout/session.py",
        "cloud_functions/creative_fanout/requirements.txt",
    }
    shipped = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in CRF_DIR.iterdir()
        if p.suffix == ".py" or p.name == "requirements.txt"
    }
    assert allowed == shipped
