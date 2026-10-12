"""The timely slot's rules (trend-state awareness PR 3): MAX_TIMELY_CONCEPTS,
build_timely_rules (from the validated moment + the tone playbook), the
`timely_rules` seeding, and the `{timely_rules?}` token in the visual and ad
copy prompts — runs without a timely moment get byte-identical instructions."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from creative_agent import prompts
from creative_agent.config import (
    DEFAULT_MAX_TIMELY_CONCEPTS,
    ResearchConfiguration,
    parse_max_timely_concepts,
)
from creative_agent.trend_situation import (
    moment_phrase,
    trend_situation_state_delta,
    validate_situation,
)

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 11, 10, 0, tzinfo=NY)
SOURCES = {"src-1": {"url": "https://example.com/a"}}
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
    "source_ids": ["src-1"],
    "confidence": "high",
}


def _situation(draft):
    return validate_situation(
        draft, sources=SOURCES, now=NOW, window_days=7, valid_hours_after=48
    )


# --- config --- #
def test_max_timely_concepts_knob():
    assert DEFAULT_MAX_TIMELY_CONCEPTS == 1
    assert parse_max_timely_concepts(None) == 1
    assert parse_max_timely_concepts("junk") == 1
    assert parse_max_timely_concepts("0") == 0
    assert parse_max_timely_concepts("2") == 2
    assert parse_max_timely_concepts("9") == 2
    assert parse_max_timely_concepts("-1") == 0


def test_max_timely_concepts_env_and_deploy(monkeypatch):
    import deployment.deploy_agent as da

    monkeypatch.setenv("MAX_TIMELY_CONCEPTS", "2")
    assert ResearchConfiguration().max_timely_concepts == 2
    assert da.ENV_VAR_DICT["MAX_TIMELY_CONCEPTS"]
    env_example = (Path(__file__).parent.parent / ".env.example").read_text()
    assert "MAX_TIMELY_CONCEPTS" in env_example


# --- rules text --- #
def test_moment_phrase_per_phase():
    assert (
        moment_phrase(_situation(UPCOMING)) == "Texas vs Oklahoma is tomorrow (Oct 12)"
    )
    assert moment_phrase(_situation(LOSS)) == (
        "Texas vs Oklahoma was yesterday (Oct 10): Texas lost to Oklahoma 24-31."
    )
    assert moment_phrase(_situation({**UPCOMING, "event_start": "2026-10-11"})) == (
        "Texas vs Oklahoma is today (Oct 11)"
    )
    assert moment_phrase(_situation({**UPCOMING, "event_start": "2026-10-14"})) == (
        "Texas vs Oklahoma is in 3 days (Oct 14)"
    )


def test_rules_for_an_upcoming_moment():
    rules = prompts.build_timely_rules(_situation(UPCOMING), 1)
    assert "At most 1 of the 4" in rules
    assert "Texas vs Oklahoma is tomorrow (Oct 12)" in rules
    assert "`uses_trend_state`" in rules and "`timely_reason`" in rules
    assert "anticipation" in rules and "never predict the score" in rules
    assert "big game" in rules
    assert "logos, wordmarks, mascots, player names or likenesses" in rules
    assert "sponsorship or endorsement" in rules
    assert "stays evergreen" in rules
    assert "{" not in rules and "}" not in rules


def test_evergreen_rule_lists_every_term_the_gates_check():
    # The copy gate and concept guard flag an evergreen creative with any
    # temporal_terms hit, so the rule names the whole lexicon (+ dates and
    # weekday names), not a sample.
    from creative_agent.temporal_terms import TEMPORAL_TERMS

    rules = prompts.build_timely_rules(_situation(UPCOMING), 1)
    evergreen = next(line for line in rules.split("\n") if "stays evergreen" in line)
    for term in TEMPORAL_TERMS:
        assert re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", evergreen), term
    assert "weekday names" in evergreen and "dates" in evergreen


def test_rules_for_a_loss_use_the_empathy_playbook():
    rules = prompts.build_timely_rules(_situation(LOSS), 2)
    assert "At most 2 of the 4" in rules
    assert "Texas lost to Oklahoma 24-31." in rules
    assert "empathy" in rules and "discount" in rules
    assert "never the words lose or lost in a headline" in rules


def test_rules_accept_a_dict_and_are_empty_when_not_timely():
    assert prompts.build_timely_rules(_situation(UPCOMING).model_dump(), 1)
    for draft in (
        None,
        {"phase": "sensitive", "confidence": "high"},
        {**UPCOMING, "phase": "live"},
        {**UPCOMING, "confidence": "low"},
    ):
        assert prompts.build_timely_rules(_situation(draft), 1) == ""
    assert prompts.build_timely_rules(_situation(UPCOMING), 0) == ""
    assert prompts.build_timely_rules(None, 1) == ""


def test_validator_delta_seeds_timely_rules_only_when_timely():
    base = {
        "sources": SOURCES,
        "current_datetime": "2026-10-11T10:00:00-04:00",
        "current_timezone": "America/New_York",
    }
    kwargs = {"enabled": True, "window_days": 7, "valid_hours_after": 48}
    timely = trend_situation_state_delta(
        {**base, "trend_situation_draft": UPCOMING}, max_timely=1, **kwargs
    )
    assert timely["timely_rules"] == prompts.build_timely_rules(
        timely["trend_situation"], 1
    )
    assert timely["timely_rules"]
    off = trend_situation_state_delta(
        {**base, "trend_situation_draft": UPCOMING}, max_timely=0, **kwargs
    )
    assert off["timely_rules"] == ""
    unknown = trend_situation_state_delta(base, max_timely=1, **kwargs)
    assert unknown["timely_rules"] == ""


def test_state_init_defaults_timely_rules_to_empty():
    from creative_agent.callbacks import _set_initial_states

    target: dict = {}
    _set_initial_states({}, target)
    assert target["timely_rules"] == ""
    assert target["rating_limits"] == ""
    seeded: dict = {"timely_rules": "kept"}
    _set_initial_states({}, seeded)
    assert seeded["timely_rules"] == "kept"


# --- prompts: the token, and byte-identical instructions without it --- #
def _timely_prompts() -> dict[str, str]:
    from interactive_creative import prompts as ic_prompts

    names = (
        "ART_DIRECTOR_INSTR",
        "VISUAL_CONCEPT_DRAFTER_INSTR",
        "VISUAL_CONCEPT_CRITIC_INSTR",
        "VISUAL_CONCEPT_FINALIZER_INSTR",
        "VISUAL_CONCEPT_FIXER_INSTR",
        "AD_COPY_DRAFTER_INSTR",
        "AD_COPY_CRITIC_INSTR",
        "AD_COPY_REVISER_INSTR",
    )
    return {
        **{name: getattr(prompts, name) for name in names},
        "VISUAL_CONCEPT_REVISER_INSTR": ic_prompts.VISUAL_CONCEPT_REVISER_INSTR,
    }


# sha256[:16] of each instruction as it was before the timely slot (main at
# 4920d1e, incl. the date line). With `timely_rules` "" the token renders to nothing, so the
# instruction the model sees is exactly this text. If one of these prompts is
# changed for another reason, update its hash in the same PR (print
# hashlib.sha256(instr.replace("{timely_rules?}", "")
# .replace("{rating_limits?}", "").encode()).hexdigest()[:16]). `rating_limits`
# (opt-in rating learning's stricter limits) is "" too unless the checks
# effect flagged something, so it is stripped the same way.
_PRE_TIMELY_SHA256 = {
    "ART_DIRECTOR_INSTR": "760ea3944a0b921e",
    "VISUAL_CONCEPT_DRAFTER_INSTR": "9fac29b2d5a9ee91",
    "VISUAL_CONCEPT_CRITIC_INSTR": "22bfd5d4f32ea53f",
    "VISUAL_CONCEPT_FINALIZER_INSTR": "5340500bdf98301b",
    "VISUAL_CONCEPT_FIXER_INSTR": "6f85b3f3fa28d0a4",
    "AD_COPY_DRAFTER_INSTR": "073a37a87da2095f",
    "AD_COPY_CRITIC_INSTR": "bf2cdcefacece0ec",
    "AD_COPY_REVISER_INSTR": "dd97709c6445e454",
    "VISUAL_CONCEPT_REVISER_INSTR": "6584de5c667ff326",
}


@pytest.mark.parametrize("name", list(_PRE_TIMELY_SHA256))
def test_instructions_without_timely_rules_are_byte_identical(name):
    instr = _timely_prompts()[name]
    without = instr.replace("{timely_rules?}", "").replace("{rating_limits?}", "")
    assert (
        hashlib.sha256(without.encode()).hexdigest()[:16] == (_PRE_TIMELY_SHA256[name])
    ), name


@pytest.mark.parametrize("name", list(_PRE_TIMELY_SHA256))
def test_timely_rules_come_only_from_state(name):
    instr = _timely_prompts()[name]
    assert instr.count("{timely_rules?}") == 1, name
    # The last thing in the instructions, after the casting rules when present
    # and followed only by the rating limits' own token where read.
    end = "{rating_limits?}" if name in _RATING_LIMITS_READERS else ""
    assert "{timely_rules?}" + end + "\n    </INSTRUCTIONS>" in instr, name
    if "{person_casting_rules?}" in instr:
        assert "{person_casting_rules?}{timely_rules?}" in instr, name
    without = instr.replace("{timely_rules?}", "")
    for word in ("uses_trend_state", "timely_reason", "current moment"):
        assert word not in without, (name, word)


# The producers that check or fix against the gates (not the drafters that see
# the full `rating_signals` note) read the run's stricter limits.
_RATING_LIMITS_READERS = frozenset(
    {
        "VISUAL_CONCEPT_DRAFTER_INSTR",
        "VISUAL_CONCEPT_CRITIC_INSTR",
        "VISUAL_CONCEPT_FINALIZER_INSTR",
        "VISUAL_CONCEPT_FIXER_INSTR",
        "AD_COPY_CRITIC_INSTR",
        "AD_COPY_REVISER_INSTR",
        "VISUAL_CONCEPT_REVISER_INSTR",
    }
)


def test_only_the_gated_producers_read_rating_limits():
    from interactive_creative import prompts as ic_prompts

    for module in (prompts, ic_prompts):
        for name in dir(module):
            value = getattr(module, name)
            if name.endswith("_INSTR") and isinstance(value, str):
                count = value.count("{rating_limits?}")
                assert count == (name in _RATING_LIMITS_READERS), name
                assert "{rating_limits}" not in value, name


def test_only_the_creative_agents_read_timely_rules():
    readers = set(_timely_prompts())
    for name in dir(prompts):
        value = getattr(prompts, name)
        if name.endswith("_INSTR") and isinstance(value, str):
            assert ("{timely_rules?}" in value) == (name in readers), name


def test_timely_rules_token_is_optional():
    for name, instr in _timely_prompts().items():
        assert not re.search(r"\{timely_rules\}", instr), name
