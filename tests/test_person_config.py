"""creative_agent/config.py: the person-casting knobs (PERSON_SAFE_STYLES,
MAX_CAST_CONCEPTS) and their deploy wiring."""

from __future__ import annotations

from pathlib import Path

from creative_agent.config import (
    DEFAULT_MAX_CAST_CONCEPTS,
    DEFAULT_PERSON_SAFE_STYLES,
    ResearchConfiguration,
    parse_max_cast_concepts,
    parse_person_safe_styles,
)


def test_calibrated_defaults():
    assert DEFAULT_PERSON_SAFE_STYLES == (
        "Candid 35mm film photo",
        "Photoreal / editorial",
        "Cinematic film still",
    )
    assert DEFAULT_MAX_CAST_CONCEPTS == 2
    assert parse_person_safe_styles(None) == DEFAULT_PERSON_SAFE_STYLES
    assert parse_max_cast_concepts(None) == 2


def test_safe_styles_parse_canonical_names_only():
    assert parse_person_safe_styles("photoreal / editorial, Anime / manga") == (
        "Photoreal / editorial",
        "Anime / manga",
    )
    # Duplicates collapse; unknown names are dropped.
    assert parse_person_safe_styles(
        "Cinematic film still,cinematic film still, nope"
    ) == ("Cinematic film still",)
    # Blank or only-unknown → the calibrated default.
    assert parse_person_safe_styles("") == DEFAULT_PERSON_SAFE_STYLES
    assert parse_person_safe_styles("nope, zilch") == DEFAULT_PERSON_SAFE_STYLES


def test_max_cast_is_clamped():
    assert parse_max_cast_concepts("junk") == 2
    assert parse_max_cast_concepts("0") == 0
    assert parse_max_cast_concepts("-1") == 0
    assert parse_max_cast_concepts("9") == 4
    assert parse_max_cast_concepts("1") == 1


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("PERSON_SAFE_STYLES", "Photoreal / editorial")
    monkeypatch.setenv("MAX_CAST_CONCEPTS", "1")
    cfg = ResearchConfiguration()
    assert cfg.person_safe_styles == ("Photoreal / editorial",)
    assert cfg.max_cast_concepts == 1


def test_shipped_to_agent_engine_and_documented():
    import deployment.deploy_agent as da

    env_example = (Path(__file__).parent.parent / ".env.example").read_text()
    for key in ("PERSON_SAFE_STYLES", "MAX_CAST_CONCEPTS"):
        assert da.ENV_VAR_DICT[key], key
        assert key in env_example, key
