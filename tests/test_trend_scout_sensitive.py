"""trend_scout's sensitive-trend classifier (`classify_trends_agent` →
`trend_flags`), run right after gathering so both the auto pick and the
interactive pick panel can see which raw trends are sensitive."""

from trend_scout.schemas import TrendFlags


def test_trend_flags_schema():
    f = TrendFlags.model_validate(
        {
            "flags": [
                {
                    "term": "x",
                    "sensitive": True,
                    "reason": "death of a public figure's child",
                }
            ]
        }
    )
    assert f.flags[0].sensitive is True


def test_trend_flags_defaults():
    f = TrendFlags.model_validate({"flags": [{"term": "y"}]})
    assert f.flags[0].sensitive is False
    assert f.flags[0].reason == ""
    assert TrendFlags().flags == []


def test_root_has_classify_tool():
    from trend_scout.agent import root_agent

    assert "classify_trends_agent" in {getattr(t, "name", "") for t in root_agent.tools}


def test_classifier_agent_wiring():
    from trend_scout.agent import classify_trends_agent
    from trend_scout.config import INFRA_RETRY, config

    assert classify_trends_agent.output_schema is TrendFlags
    assert classify_trends_agent.output_key == "trend_flags"
    assert classify_trends_agent.include_contents == "none"
    assert classify_trends_agent.retry_config is INFRA_RETRY
    assert config.gather_model in str(classify_trends_agent.model.model)


def test_classifier_instruction():
    from trend_scout.prompts import CLASSIFY_TRENDS_INSTR

    assert "{raw_gtrends?}" in CLASSIFY_TRENDS_INSTR
    # brace-free apart from the optional state tokens
    stripped = CLASSIFY_TRENDS_INSTR
    for token in (
        "{raw_gtrends?}",
        "{current_date?}",
        "{current_weekday?}",
        "{current_timezone?}",
    ):
        stripped = stripped.replace(token, "")
    assert "{" not in stripped and "}" not in stripped
    flat = " ".join(CLASSIFY_TRENDS_INSTR.split())
    assert "verbatim" in flat
    assert "serious illness" in flat
    assert "not sensitive" in flat
