"""Bandit image builds: both Dockerfiles cache their dependency layer."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = {
    "cpr": REPO_ROOT / "bandit_serving" / "Dockerfile",
    "traffic": REPO_ROOT / "bandit_traffic" / "Dockerfile",
}


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


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_dependencies_install_before_code(name):
    ins = _instructions(DOCKERFILES[name])
    installs = [i for i, (k, a) in enumerate(ins) if k == "RUN" and "pip install" in a]
    assert len(installs) == 1, installs
    copies = [(i, a) for i, (k, a) in enumerate(ins) if k == "COPY"]
    before = [a for i, a in copies if i < installs[0] and "--from" not in a]
    assert len(before) == 1 and "requirements.txt" in before[0], before
    code = [i for i, a in copies if "--from" not in a and "requirements.txt" not in a]
    assert code and min(code) > installs[0]


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_multi_stage_uv_venv_non_root(name):
    ins = _instructions(DOCKERFILES[name])
    froms = [a for k, a in ins if k == "FROM"]
    assert len(froms) == 3, froms  # uv, builder, final
    assert "uv venv /opt/venv" in next(a for k, a in ins if k == "RUN" and "uv " in a)
    assert ("COPY", "--from=builder /opt/venv /opt/venv") in ins
    last_from = max(i for i, (k, _) in enumerate(ins) if k == "FROM")
    users = [a for k, a in ins[last_from:] if k == "USER"]
    assert users and users[-1] not in ("root", "0"), users


def test_traffic_entrypoint_unchanged():
    ins = _instructions(DOCKERFILES["traffic"])
    assert ins[-1] == ("ENTRYPOINT", '["python", "-m", "bandit_traffic.main"]')
    assert ("USER", "app") in ins
