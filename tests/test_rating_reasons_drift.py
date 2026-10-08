"""runserver.rating_reasons: the fail-reason enum + labels, mirrored in
frontend/src/lib/rating-reasons.ts (drift test, like test_eval_dimensions.py)."""

import pathlib
import re

from runserver.rating_reasons import FAIL_REASON_LABELS, FAIL_REASONS

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
