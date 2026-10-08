"""runserver.rating_reasons: the fail-reason enum + labels, mirrored in
frontend/src/lib/rating-reasons.ts (drift test, like test_eval_dimensions.py)."""

import pathlib
import re

from runserver.rating_reasons import (
    FAIL_REASON_LABELS,
    FAIL_REASONS,
    FAIL_REASONS_BY_KIND,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TS_MAP = REPO_ROOT / "frontend" / "src" / "lib" / "rating-reasons.ts"


def _block(name: str, closer: str) -> str:
    m = re.search(rf"{name}[^=]*=\s*[\[{{](.*?)\n{closer}", TS_MAP.read_text(), re.S)
    assert m is not None, name
    return m.group(1)


def test_labels_match_frontend_map_in_order():
    pairs = re.findall(
        r'^\s*(\w+):\s*"([^"]+)",?\s*$', _block("FAIL_REASON_LABELS", r"\};"), re.M
    )
    assert pairs == list(FAIL_REASON_LABELS.items())


def test_enum_matches_frontend_list_in_order():
    values = re.findall(r'^\s*"(\w+)",?\s*$', _block("FAIL_REASONS", r"\]"), re.M)
    assert tuple(values) == FAIL_REASONS
    assert tuple(FAIL_REASON_LABELS) == FAIL_REASONS


def test_per_kind_lists_match_frontend():
    """failReasonsFor(kind) = FAIL_REASONS minus the other kind's *_ONLY set."""

    def ts_set(name: str) -> set[str]:
        m = re.search(rf"{name}[^=]*=\s*new Set\(\[(.*?)\]\)", TS_MAP.read_text(), re.S)
        assert m is not None, name
        return set(re.findall(r'"(\w+)"', m.group(1)))

    visual_only, copy_only = ts_set("VISUAL_ONLY"), ts_set("COPY_ONLY")
    ts_by_kind = {
        "visual": tuple(r for r in FAIL_REASONS if r not in copy_only),
        "ad_copy": tuple(r for r in FAIL_REASONS if r not in visual_only),
    }
    assert ts_by_kind == FAIL_REASONS_BY_KIND


def test_strictness_labels_match_the_agent_strictness_reasons():
    """frontend STRICTNESS_LABELS keys = creative_agent STRICTNESS_REASONS
    (the check-backed reasons a learned run can tighten), in order."""
    from creative_agent.rating_signals import STRICTNESS_REASONS

    keys = re.findall(
        r'^\s*(\w+):\s*"[^"]+",?\s*$', _block("STRICTNESS_LABELS", r"\};"), re.M
    )
    assert tuple(keys) == STRICTNESS_REASONS
