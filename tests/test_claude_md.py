"""CLAUDE.md stays a short index; subsystem detail lives in docs/reference/.

CLAUDE.md is loaded into every agent session and kept for every message, so its
size is paid on every turn. At 166 KB (about 67k tokens, 2026-10-15) it had grown
into a full design spec; the detail now lives in on-demand reference docs that
CLAUDE.md points to.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = ROOT / "CLAUDE.md"
REFERENCE_DIR = ROOT / "docs" / "reference"
MAX_BYTES = 45_000  # about 18k tokens


def _text() -> str:
    return CLAUDE_MD.read_text(encoding="utf-8")


def test_claude_md_stays_small():
    size = len(CLAUDE_MD.read_bytes())
    assert size <= MAX_BYTES, (
        f"CLAUDE.md is {size} bytes (limit {MAX_BYTES}): move subsystem detail "
        "into docs/reference/ and leave a one-line pointer"
    )


def test_reference_links_resolve_and_no_orphans():
    linked = set(re.findall(r"docs/reference/([\w.-]+\.md)", _text()))
    existing = {p.name for p in REFERENCE_DIR.glob("*.md")}
    assert linked, "CLAUDE.md links no reference docs"
    assert linked <= existing, f"missing reference docs: {sorted(linked - existing)}"
    assert existing <= linked, f"reference docs not linked: {sorted(existing - linked)}"


def test_key_invariants_stay_in_claude_md():
    text = _text()
    for marker in (
        "Package layout",
        "--no-cpu-throttling",
        "update-traffic",
        "allUsers",
        "jax",
        "JUDGE_VERSION",
        "finalize_done",
        "Co-Authored-By",
        "stable_row_id",
    ):
        assert marker in text, marker
