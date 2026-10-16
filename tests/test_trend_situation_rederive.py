"""Batch (CRF) runs re-derive the trend's current moment; the hint never decides it.

The CRF worker seeds trend_scout's ``trend_scout_context`` / ``trend_detected_on``
as hints only. ``trend_situation`` comes from the grounded branch + the
deterministic validator in every creative run, so a stale or misleading hint
can never make a creative timely. These tests pin that:

* the branch's code and prompts never read the hint keys;
* the validator's output is the same with or without a misleading hint;
* the worker seeds no ``trend_situation`` (or any of its derived keys).

Not named ``test_crf_*``: the CRF-only CI venv has no ``creative_agent``.
"""

from __future__ import annotations

import re

from cloud_functions.creative_fanout import worker as crf_worker
from creative_agent import prompts
from creative_agent.trend_situation import trend_situation_state_delta
from tests.repo_layout import package_dir

CREATIVE_AGENT = package_dir("creative_agent")
HINT_KEYS = ("trend_scout_context", "trend_detected_on")
SITUATION_KEYS = {"trend_situation", "trend_situation_md", "timely_rules"}

# A hint claiming the event already happened and was won, on an old date.
MISLEADING_HINT = {
    "trend_scout_context": (
        '{"context": "Texas beat Oklahoma 45-3", "event_hint": "game was '
        'yesterday, Texas won", "hook": "Victory!"}'
    ),
    "trend_detected_on": "2026-09-01",
}
SOURCES = {"src-1": {"short_id": "src-1", "url": "https://example.com/preview"}}
UPCOMING = {
    "phase": "upcoming",
    "event_name": "Texas vs Oklahoma",
    "event_start": "2026-10-12",
    "source_ids": ["src-1"],
    "confidence": "high",
}
CLOCK = {
    "current_datetime": "2026-10-11T10:00:00-04:00",
    "current_timezone": "America/New_York",
}


def _delta(state):
    return trend_situation_state_delta(
        state, enabled=True, window_days=7, valid_hours_after=48, max_timely=1
    )


def test_situation_code_never_reads_the_hint_keys():
    files = [
        CREATIVE_AGENT / "trend_situation.py",
        *sorted((CREATIVE_AGENT / "sub_agents" / "trend_situation").glob("*.py")),
    ]
    assert len(files) >= 2
    for path in files:
        source = path.read_text()
        for key in HINT_KEYS:
            assert key not in source, f"{path.name} reads {key}"


def test_situation_prompts_never_read_the_hint_keys():
    for instr in (
        prompts.TREND_SITUATION_SEARCHER_INSTR,
        prompts.TREND_SITUATION_SYNTHESIZER_INSTR,
    ):
        tokens = set(re.findall(r"\{([^{}]+)\}", instr))
        for key in HINT_KEYS:
            assert key not in instr
            assert key not in tokens and f"{key}?" not in tokens


def test_misleading_hint_does_not_change_the_validated_moment():
    base = {"trend_situation_draft": UPCOMING, "sources": SOURCES, **CLOCK}
    with_hint = _delta({**base, **MISLEADING_HINT})
    assert with_hint == _delta(base)
    assert with_hint["trend_situation"]["phase"] == "upcoming"
    assert with_hint["trend_situation"]["outcome"] == "none"


def test_hint_alone_never_makes_a_moment():
    """No draft (the branch found nothing): the hint is not a fallback."""
    delta = _delta({"sources": SOURCES, **CLOCK, **MISLEADING_HINT})
    assert delta["trend_situation"]["phase"] == "unknown"
    assert delta["trend_situation"]["timely_ok"] is False
    assert delta["trend_situation_md"] == ""
    assert delta["timely_rules"] == ""


def test_crf_worker_seeds_no_trend_situation():
    seeded = {state_key for state_key, _ in crf_worker.CAMPAIGN_STATE_KEYS}
    assert set(HINT_KEYS) <= seeded
    assert not seeded & SITUATION_KEYS
    row = {
        "brand": "Acme",
        "target_product": "Rocket Skates",
        "key_selling_point": "fast",
        "target_audience": "coyotes",
        "target_search_trend": "texas vs oklahoma",
        "refresh_date": "2026-09-01",
        "trend_context": MISLEADING_HINT["trend_scout_context"],
        # Even a row carrying these columns seeds none of them.
        "trend_situation": '{"phase": "just_happened", "timely_ok": true}',
        "timely_rules": "x",
    }
    assert not set(crf_worker.build_campaign_state(row)) & SITUATION_KEYS
