"""runserver/timely.py: timely creatives + expiry, pinned by the shared golden table.

``tests/fixtures/timely_expiry.json`` is also read by the frontend's
``lib/timely.ts`` tests, so the Python and TS rules can't drift apart.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from runserver.timely import is_timely, timely_expired, valid_until

GOLDEN = json.loads(
    (Path(__file__).resolve().parent / "fixtures/timely_expiry.json").read_text()
)


def _now(case) -> dt.datetime:
    return dt.datetime.fromisoformat(case["now"])


@pytest.mark.parametrize("case", GOLDEN, ids=[c["case"] for c in GOLDEN])
def test_golden_table(case):
    state, name = case["state"], case["concept_name"]
    assert is_timely(state, name) is case["timely"]
    assert timely_expired(state, name, _now(case)) is case["expired"]


def test_golden_table_covers_the_contract():
    cases = {c["case"] for c in GOLDEN}
    assert len(GOLDEN) >= 10
    assert any(c["expired"] for c in GOLDEN)
    assert any(c["timely"] and not c["expired"] for c in GOLDEN)
    assert any("boundary" in c or "exactly" in c for c in cases)


@pytest.mark.parametrize("case", GOLDEN, ids=[c["case"] for c in GOLDEN])
def test_json_string_state_matches(case):
    # Real session state may hold either container as a JSON string.
    state = dict(case["state"])
    for key in ("final_visual_concepts", "ad_copy_critique", "trend_situation"):
        if key in state:
            state[key] = json.dumps(state[key])
    name = case["concept_name"]
    assert is_timely(state, name) is case["timely"]
    assert timely_expired(state, name, _now(case)) is case["expired"]


def _state(valid="2026-10-14T23:59:59-04:00", **situation):
    return {
        "final_visual_concepts": {
            "visual_concepts": [{"concept_name": "A", "uses_trend_state": True}]
        },
        "trend_situation": {"timely_ok": True, "valid_until": valid, **situation},
    }


def test_offset_less_valid_until_never_expires():
    later = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    state = _state("2026-10-14T23:59:59")
    assert is_timely(state, "A") is True
    assert timely_expired(state, "A", later) is False


@pytest.mark.parametrize("bad", [None, 3, {"x": 1}, "garbage json"])
def test_malformed_containers_are_not_timely(bad):
    state = {"final_visual_concepts": bad, "ad_copy_critique": bad}
    state["trend_situation"] = _state()["trend_situation"]
    assert is_timely(state, "A") is False
    assert is_timely({**_state(), "trend_situation": bad}, "A") is False


def test_timely_ok_must_be_exactly_true():
    assert is_timely(_state(timely_ok="yes"), "A") is False
    assert valid_until(_state("  2026-10-14T23:59:59Z  ")) == "2026-10-14T23:59:59Z"
