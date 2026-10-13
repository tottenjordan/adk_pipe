"""generate_image marks the timely concept's render record.

Only a timely concept (its own ``uses_trend_state`` or its paired copy's, under
a timely validated moment with a ``valid_until``) gets ``timely: True`` +
``valid_until``; every other record keeps exactly the pre-timely keys (the
render-parity golden pins the whole shape for runs without a moment).
"""

import json

import pytest

from creative_agent import image_tools
from tests.test_image_qa import _CONCEPT, _Flow

_VALID_UNTIL = "2026-10-14T23:59:59-04:00"
_BASE_KEYS = {"gcs_uri", "artifact_key", "attempts", "qa"}
_TIMELY_SITUATION = {"timely_ok": True, "valid_until": _VALID_UNTIL}


def _concept(name, copy_id, **extra):
    return {**_CONCEPT, "concept_name": name, "ad_copy_id": copy_id, **extra}


def _run(monkeypatch, state, concepts, *, max_timely=1):
    monkeypatch.setattr(image_tools.config, "max_timely_concepts", max_timely)
    flow = _Flow(monkeypatch, [], enabled=False, concepts=concepts)
    flow.ctx.state.update(state)
    flow.run()
    return flow.ctx.state["generated_images"]


_CONCEPTS = [
    _concept("Flagged", 1, uses_trend_state=True),
    _concept("CopyFlagged", 2),
    _concept("Evergreen", 3),
]
_COPIES = {
    "ad_copies": [
        {"original_id": 1, "uses_trend_state": False},
        {"original_id": "2", "uses_trend_state": True},
        {"original_id": 3, "uses_trend_state": False},
    ]
}


def test_only_timely_concepts_get_timely_keys(monkeypatch):
    images = _run(
        monkeypatch,
        {"trend_situation": _TIMELY_SITUATION, "ad_copy_critique": _COPIES},
        [dict(c) for c in _CONCEPTS],
    )
    for name in ("Flagged", "CopyFlagged"):
        assert images[name]["timely"] is True
        assert images[name]["valid_until"] == _VALID_UNTIL
        assert set(images[name]) == _BASE_KEYS | {"timely", "valid_until"}
    # Byte-identical for the evergreen concept, even in a timely run.
    assert set(images["Evergreen"]) == _BASE_KEYS


def test_copies_may_be_a_json_string(monkeypatch):
    images = _run(
        monkeypatch,
        {
            "trend_situation": _TIMELY_SITUATION,
            "ad_copy_critique": json.dumps(_COPIES),
        },
        [dict(c) for c in _CONCEPTS],
    )
    assert images["CopyFlagged"]["timely"] is True
    assert set(images["Evergreen"]) == _BASE_KEYS


@pytest.mark.parametrize(
    ("situation", "max_timely"),
    [
        (None, 1),
        ({"timely_ok": False, "valid_until": _VALID_UNTIL}, 1),
        ({"timely_ok": "true", "valid_until": _VALID_UNTIL}, 1),
        ({"timely_ok": True, "valid_until": ""}, 1),
        ({"timely_ok": True}, 1),
        (_TIMELY_SITUATION, 0),
    ],
)
def test_no_timely_keys_without_a_timely_moment(monkeypatch, situation, max_timely):
    state = {"ad_copy_critique": _COPIES}
    if situation is not None:
        state["trend_situation"] = situation
    images = _run(
        monkeypatch, state, [dict(c) for c in _CONCEPTS], max_timely=max_timely
    )
    for record in images.values():
        assert set(record) == _BASE_KEYS


def test_unpaired_concept_never_matches_a_copy_without_an_id(monkeypatch):
    """A concept without ad_copy_id is not paired with a copy lacking
    original_id (str(None) == str(None) must not count as a pair)."""
    images = _run(
        monkeypatch,
        {
            "trend_situation": _TIMELY_SITUATION,
            "ad_copy_critique": {"ad_copies": [{"uses_trend_state": True}]},
        },
        [{**_CONCEPT, "concept_name": "Loose"}],
    )
    assert set(images["Loose"]) == _BASE_KEYS
