"""creative_agent/trend_situation.py: the deterministic trend-situation
validator (abstain to "unknown" unless the moment is dated, sourced and inside
the window), the tone playbook and the one-line "Current moment" rendering."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from creative_agent.trend_situation import (
    current_moment_md,
    resolve_now,
    tone_rule_for,
    validate_situation,
)

NY = ZoneInfo("America/New_York")
# Sunday 2026-10-11, 10:00 in New York.
NOW = datetime(2026, 10, 11, 10, 0, tzinfo=NY)
SOURCES = {
    "src-1": {"short_id": "src-1", "url": "https://example.com/texas-preview"},
    "src-2": {
        "short_id": "src-2",
        "url": "https://news.example.com/2026/10/11/texas-falls-to-oklahoma",
    },
    "src-3": {
        "short_id": "src-3",
        "url": "https://news.example.com/2026/09/01/season-preview",
    },
}


def _validate(draft, **kwargs):
    return validate_situation(
        draft,
        sources=kwargs.pop("sources", SOURCES),
        now=kwargs.pop("now", NOW),
        window_days=kwargs.pop("window_days", 7),
        valid_hours_after=kwargs.pop("valid_hours_after", 48),
        **kwargs,
    )


UPCOMING = {
    "phase": "upcoming",
    "event_name": "Texas vs Oklahoma",
    "event_start": "2026-10-12",
    "source_ids": ["src-1"],
    "confidence": "high",
}
LOSS = {
    "phase": "just_happened",
    "event_name": "Texas vs Oklahoma",
    "event_start": "2026-10-10",
    "outcome": "loss",
    "outcome_summary": "Texas lost to Oklahoma 24-31.",
    "audience_sentiment": "negative",
    "source_ids": ["src-2"],
    "confidence": "high",
}


@pytest.mark.parametrize(
    ("draft", "phase", "timely_ok"),
    [
        # Tomorrow's game with a source -> upcoming, ok.
        (UPCOMING, "upcoming", True),
        # Today's game (later today) still counts as upcoming.
        ({**UPCOMING, "event_start": "2026-10-11T19:30:00-05:00"}, "upcoming", True),
        # Yesterday's loss with a source -> just_happened, ok.
        (LOSS, "just_happened", True),
        # An outcome with no source -> unknown.
        ({**LOSS, "source_ids": []}, "unknown", False),
        # Upcoming with no source -> unknown.
        ({**UPCOMING, "source_ids": []}, "unknown", False),
        # A date 20 days out -> unknown.
        ({**UPCOMING, "event_start": "2026-10-31"}, "unknown", False),
        # An upcoming event dated in the past -> unknown.
        ({**UPCOMING, "event_start": "2026-10-09"}, "unknown", False),
        # A just-happened event dated in the future -> unknown.
        ({**LOSS, "event_start": "2026-10-13"}, "unknown", False),
        # Just happened with no outcome -> unknown.
        ({**LOSS, "outcome": ""}, "unknown", False),
        # An unknown src id -> unknown.
        ({**UPCOMING, "source_ids": ["src-1", "src-9"]}, "unknown", False),
        # No parseable date -> unknown.
        ({**UPCOMING, "event_start": "next Saturday"}, "unknown", False),
        ({**UPCOMING, "event_start": ""}, "unknown", False),
        # A source whose URL date predates the event can't report its result.
        ({**LOSS, "source_ids": ["src-3"]}, "unknown", False),
        # Sensitive / live / ongoing / evergreen are never timely.
        (
            {"phase": "sensitive", "event_name": "Player injury", "confidence": "high"},
            "sensitive",
            False,
        ),
        ({**UPCOMING, "phase": "live"}, "live", False),
        ({"phase": "ongoing", "confidence": "high"}, "ongoing", False),
        ({"phase": "evergreen", "confidence": "high"}, "evergreen", False),
        # Low confidence -> not ok (the phase is kept).
        ({**UPCOMING, "confidence": "low"}, "upcoming", False),
        # The model's own abstention.
        ({"phase": "unknown"}, "unknown", False),
        # No draft at all (failed / empty branch).
        (None, "unknown", False),
        ("", "unknown", False),
        ({"phase": "not-a-phase"}, "unknown", False),
    ],
)
def test_validator_table(draft, phase, timely_ok):
    situation = _validate(draft)
    assert situation.phase == phase
    assert situation.timely_ok is timely_ok
    if timely_ok:
        assert situation.abstain_reason == ""
        assert situation.tone_rule
    else:
        assert situation.abstain_reason
        assert situation.tone_rule == ""


def test_draft_as_json_string_is_parsed():
    assert _validate(json.dumps(UPCOMING)).timely_ok is True


def test_disabled_abstains():
    situation = _validate(UPCOMING, enabled=False)
    assert situation.phase == "unknown"
    assert situation.timely_ok is False
    assert situation.abstain_reason == "disabled"


def test_upcoming_valid_until_is_end_of_event_day():
    situation = _validate(UPCOMING)
    assert situation.valid_until == "2026-10-12T23:59:59-04:00"
    assert situation.as_of == "2026-10-11T10:00:00-04:00"


def test_just_happened_valid_until_is_48_hours_after_the_event():
    situation = _validate(LOSS)
    # End of the event day (2026-10-10 23:59:59) + 48 h.
    assert situation.valid_until == "2026-10-12T23:59:59-04:00"
    with_time = _validate({**LOSS, "event_start": "2026-10-10T20:00:00-04:00"})
    assert with_time.valid_until == "2026-10-12T20:00:00-04:00"
    with_end = _validate(
        {
            **LOSS,
            "event_start": "2026-10-09T20:00:00-04:00",
            "event_end": "2026-10-10T01:00:00-04:00",
        }
    )
    assert with_end.valid_until == "2026-10-12T01:00:00-04:00"


def test_expired_just_happened_moment_is_not_timely():
    situation = _validate({**LOSS, "event_start": "2026-10-07"})
    assert situation.phase == "just_happened"
    assert situation.timely_ok is False
    assert "expired" in situation.abstain_reason


def test_window_knob():
    assert _validate({**UPCOMING, "event_start": "2026-10-15"}).timely_ok is True
    narrow = _validate({**UPCOMING, "event_start": "2026-10-15"}, window_days=2)
    assert narrow.phase == "unknown"


def test_sensitive_keeps_only_known_sources():
    situation = _validate(
        {"phase": "sensitive", "source_ids": ["src-1", "src-9"], "confidence": "high"}
    )
    assert situation.phase == "sensitive"
    assert situation.source_ids == ["src-1"]


def test_utc_datetime_is_read_in_the_run_timezone():
    # 03:00 UTC on the 12th is 23:00 on the 11th in New York: today, not tomorrow.
    situation = _validate({**UPCOMING, "event_start": "2026-10-12T03:00:00Z"})
    assert situation.valid_until == "2026-10-11T23:59:59-04:00"


# --- tone playbook --- #
def test_tone_rule_table():
    upcoming = tone_rule_for("upcoming", "")
    assert "anticipation" in upcoming and "score" in upcoming
    win = tone_rule_for("just_happened", "win")
    assert "celebration" in win and "gloat" in win
    loss = tone_rule_for("just_happened", "loss")
    assert "empathy" in loss and "discount" in loss and "headline" in loss
    assert "factual" in tone_rule_for("just_happened", "other")
    assert "factual" in tone_rule_for("just_happened", "draw")
    for phase in ("live", "sensitive", "ongoing", "evergreen", "unknown"):
        assert tone_rule_for(phase, "") == ""


def test_tone_rules_are_brace_free():
    for phase, outcome in (
        ("upcoming", ""),
        ("just_happened", "win"),
        ("just_happened", "loss"),
        ("just_happened", "other"),
    ):
        rule = tone_rule_for(phase, outcome)
        assert "{" not in rule and "}" not in rule


# --- one-line rendering --- #
def test_current_moment_md_for_timely_moment():
    md = current_moment_md(_validate(LOSS))
    assert md.startswith("Just happened: Texas vs Oklahoma, 2026-10-10.")
    assert "Texas lost to Oklahoma 24-31." in md
    assert "sources: src-2" in md
    assert "valid until 2026-10-12 23:59" in md
    assert "\n" not in md


def test_current_moment_md_without_the_timely_slot_has_no_valid_until():
    """MAX_TIMELY_CONCEPTS=0: the moment is still stated as context, but without
    the "valid until" time that invites the brief writer to set a timely angle
    (TIMELY_ANGLE_RULE) the brief gate would then reject."""
    md = current_moment_md(_validate(LOSS), max_timely=0)
    assert md.startswith("Just happened: Texas vs Oklahoma, 2026-10-10.")
    assert "sources: src-2" in md
    assert "valid until" not in md


def test_state_delta_passes_max_timely_to_the_moment_line():
    from creative_agent.trend_situation import trend_situation_state_delta

    state = {
        "trend_situation_draft": UPCOMING,
        "sources": SOURCES,
        "current_datetime": "2026-10-11T10:00:00-04:00",
        "current_timezone": "America/New_York",
    }
    kwargs = {"enabled": True, "window_days": 7, "valid_hours_after": 48}
    on = trend_situation_state_delta(state, max_timely=1, **kwargs)
    off = trend_situation_state_delta(state, max_timely=0, **kwargs)
    assert "valid until" in on["trend_situation_md"]
    assert off["trend_situation_md"].startswith("Upcoming:")
    assert "valid until" not in off["trend_situation_md"]


def test_current_moment_md_for_sensitive_moment_says_never_reference():
    md = current_moment_md(
        _validate(
            {
                "phase": "sensitive",
                "event_name": "Stadium collapse",
                "source_ids": ["src-1"],
                "confidence": "high",
            }
        )
    )
    assert md.startswith("Sensitive: Stadium collapse.")
    assert "never reference" in md.lower()


@pytest.mark.parametrize(
    "draft",
    [
        None,
        {"phase": "ongoing", "confidence": "high"},
        {**UPCOMING, "confidence": "low"},
    ],
)
def test_current_moment_md_is_empty_when_not_usable(draft):
    assert current_moment_md(_validate(draft)) == ""


def test_current_moment_md_strips_braces():
    md = current_moment_md(_validate({**UPCOMING, "event_name": "Big {game}"}))
    assert "{" not in md and "}" not in md


def test_current_moment_md_accepts_a_dict():
    assert current_moment_md(_validate(UPCOMING).model_dump()).startswith("Upcoming:")
    assert current_moment_md(None) == ""


# --- now --- #
def test_resolve_now_prefers_state_datetime():
    now = resolve_now({"current_datetime": "2026-10-11T23:00:00-04:00"})
    assert now.isoformat() == "2026-10-11T23:00:00-04:00"


def test_resolve_now_falls_back_to_the_timezone():
    now = resolve_now({"current_datetime": "garbage"}, tz_name="America/New_York")
    assert now.tzinfo is not None
    assert resolve_now({}, tz_name="Mars/Base").tzinfo is not None


def test_state_delta_reads_draft_sources_and_state_clock():
    from creative_agent.trend_situation import trend_situation_state_delta

    delta = trend_situation_state_delta(
        {
            "trend_situation_draft": UPCOMING,
            "sources": SOURCES,
            "current_datetime": "2026-10-11T10:00:00-04:00",
            "current_timezone": "America/New_York",
        },
        enabled=True,
        window_days=7,
        valid_hours_after=48,
    )
    assert delta["trend_situation"]["phase"] == "upcoming"
    assert delta["trend_situation"]["timely_ok"] is True
    assert delta["trend_situation_md"].startswith("Upcoming:")


def test_situation_prompts_use_only_known_tokens():
    """Every state token the situation prompts read; the date tokens and the
    upstream outputs are optional so a run without them never KeyErrors."""
    import re

    from creative_agent.prompts import (
        TREND_SITUATION_SEARCHER_INSTR,
        TREND_SITUATION_SYNTHESIZER_INSTR,
    )

    date_tokens = {"current_date?", "current_weekday?", "current_timezone?"}
    tokens = re.compile(r"\{([^{}]+)\}")
    assert set(tokens.findall(TREND_SITUATION_SEARCHER_INSTR)) == date_tokens | {
        "target_search_trends",
        "target_audience",
    }
    assert set(tokens.findall(TREND_SITUATION_SYNTHESIZER_INSTR)) == date_tokens | {
        "target_search_trends",
        "target_audience",
        "trend_situation_raw?",
        "sources?",
    }
    assert "unknown" in TREND_SITUATION_SYNTHESIZER_INSTR
    assert "sensitive" in TREND_SITUATION_SYNTHESIZER_INSTR


def test_situation_outcome_is_from_the_audience_side():
    """The tone playbook (tone_rule_for) speaks to the audience's side, so the
    outcome must be scored from the side the target audience supports."""
    from creative_agent.prompts import TREND_SITUATION_SYNTHESIZER_INSTR
    from creative_agent.schemas import TrendSituationDraft

    instr = TREND_SITUATION_SYNTHESIZER_INSTR
    assert "the side the target audience supports" in instr
    assert "first-named" in instr
    assert "trend subject's side" not in instr
    desc = TrendSituationDraft.model_fields["outcome"].description or ""
    assert "target audience" in desc


def test_situation_searcher_freshness_follows_the_event_window():
    from creative_agent.config import config
    from creative_agent.prompts import TREND_SITUATION_SEARCHER_INSTR

    days = config.trend_event_window_days
    assert f"published in the last {days} days" in TREND_SITUATION_SEARCHER_INSTR


def test_state_delta_without_a_draft_abstains():
    from creative_agent.trend_situation import trend_situation_state_delta

    delta = trend_situation_state_delta(
        {}, enabled=True, window_days=7, valid_hours_after=48
    )
    assert delta["trend_situation"]["phase"] == "unknown"
    assert delta["trend_situation_md"] == ""


# --- CA-1: a trend flagged sensitive by trend_scout is never timely --- #
_TRIAL = {
    "phase": "upcoming",
    "event_name": "Tupac Shakur murder trial",
    "event_start": "2026-10-12",
    "source_ids": ["src-1"],
    "confidence": "high",
}


def _trial_state(**extra):
    return {
        "trend_situation_draft": _TRIAL,
        "sources": SOURCES,
        "current_datetime": "2026-10-11T10:00:00-04:00",
        "current_timezone": "America/New_York",
        **extra,
    }


def _trial_delta(state):
    from creative_agent.trend_situation import trend_situation_state_delta

    return trend_situation_state_delta(
        state, enabled=True, window_days=7, valid_hours_after=48, max_timely=1
    )


def test_trend_scout_sensitive_hint_floors_the_moment_to_not_timely():
    from creative_agent.brief_check import check_brief
    from creative_agent.trend_situation import timely_allowed
    from tests.test_creative_agent_graph import _BRIEF

    # Without the hint the drafted trial is a valid upcoming moment.
    plain = _trial_delta(_trial_state())
    assert plain["trend_situation"]["timely_ok"] is True
    assert "Tupac Shakur murder trial" in plain["timely_rules"]

    state = _trial_state(trend_sensitive_hint="yes")
    delta = _trial_delta(state)
    situation = delta["trend_situation"]
    assert situation["phase"] == "upcoming"  # only timely_ok is floored
    assert situation["timely_ok"] is False
    assert situation["abstain_reason"] == "flagged sensitive by trend scout"
    assert situation["tone_rule"] == ""
    assert delta["timely_rules"] == ""
    assert "valid until" not in delta["trend_situation_md"]

    # The guards agree, even on a state whose stored moment still says timely.
    assert timely_allowed({**state, **plain}, 1) is False
    assert timely_allowed({**plain}, 1) is True

    brief = json.loads(json.dumps(_BRIEF))
    brief["trend_bridge"]["timely_angle"] = "Count down to the trial tomorrow"
    issues = check_brief(brief, trend_situation=situation, max_timely=1)
    assert any("timely_angle" in i for i in issues)


@pytest.mark.parametrize("hint", ["", "no", None])
def test_other_sensitive_hint_values_do_not_floor(hint):
    delta = _trial_delta(_trial_state(trend_sensitive_hint=hint))
    assert delta["trend_situation"]["timely_ok"] is True


# --- CA-7 boundaries (M12 window, M13 source dated event - 1 day) --- #
def test_event_exactly_window_days_away_is_inside_the_window():
    # NOW is 2026-10-11; window 7 -> 2026-10-18 is in, 2026-10-19 is out.
    assert _validate({**UPCOMING, "event_start": "2026-10-18"}).timely_ok is True
    beyond = _validate({**UPCOMING, "event_start": "2026-10-19"})
    assert beyond.phase == "unknown"
    assert "more than 7 days" in beyond.abstain_reason


def test_source_dated_the_day_before_the_event_is_accepted():
    sources = {
        "src-1": {"url": "https://news.example.com/2026/10/09/texas-oklahoma"},
        "src-2": {"url": "https://news.example.com/2026/10/08/texas-oklahoma"},
    }
    day_before = _validate({**LOSS, "source_ids": ["src-1"]}, sources=sources)
    assert day_before.timely_ok is True
    two_days = _validate({**LOSS, "source_ids": ["src-2"]}, sources=sources)
    assert two_days.phase == "unknown"
    assert "predates the event" in two_days.abstain_reason
