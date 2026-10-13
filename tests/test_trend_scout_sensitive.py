"""trend_scout's sensitive-trend classifier (`classify_trends_agent` →
`trend_flags`), run inside the `trend_digest` NodeTool right after gathering so
both the auto pick and the interactive pick panel can see which raw trends are
sensitive."""

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


def test_root_has_digest_tool_with_the_classifier_inside():
    from tests._fakes import walk_nodes
    from trend_scout.agent import root_agent, trend_digest

    names = {getattr(t, "name", "") for t in root_agent.tools}
    assert "trend_digest" in names
    assert "classify_trends_agent" not in names  # no longer a top-level tool
    assert "classify_trends_agent" in {n.name for n in walk_nodes(trend_digest)}


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
    # the describe step's live summaries (optional: empty renders as "")
    assert "{trend_summaries_draft?}" in CLASSIFY_TRENDS_INSTR
    # brace-free apart from the optional state tokens
    stripped = CLASSIFY_TRENDS_INSTR
    for token in (
        "{raw_gtrends?}",
        "{trend_summaries_draft?}",
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


# --- Task 1.2: root flow, auto pick, save guard ---------------------------


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def test_root_digests_right_after_gather_in_both_branches():
    from trend_scout.prompts import TREND_SCOUT_INSTR

    instr = _flat(TREND_SCOUT_INSTR).lower()
    gather = instr.index("call `gather_trends_agent`")
    digest = instr.index("call `trend_digest`")
    branch = instr.index("the interactive trend-picking flag is")
    review = instr.index("call `review_trends`")
    research = instr.index("call `understand_trends_agent_resilient`")
    # once, shared by both branches: after gather, before the branch (and so
    # before the interactive review_trends pause and the auto research).
    assert gather < digest < branch < review
    assert digest < research
    assert instr.count("call `trend_digest`") == 1
    assert "classify_trends_agent" not in instr


def test_pick_instruction_never_selects_flagged_terms():
    from trend_scout.prompts import PICK_TRENDS_INSTR

    assert "{trend_flags?}" in PICK_TRENDS_INSTR
    flat = _flat(PICK_TRENDS_INSTR)
    assert "Never select a term flagged sensitive" in flat
    assert "serious illness" in flat


def _ctx(flags, *, interactive=False):
    from tests._fakes import FakeToolContext

    ctx = FakeToolContext()
    ctx.state["target_search_trends"] = {"target_search_trends": []}
    ctx.state["interactive_trend_pick"] = interactive
    if flags is not None:
        ctx.state["trend_flags"] = flags
    return ctx


FLAGS = {
    "flags": [
        {"term": "Storm Ada", "sensitive": True, "reason": "deadly storm"},
        {"term": "Golden Dip", "sensitive": False, "reason": ""},
    ]
}


def _saved(ctx):
    return ctx.state["target_search_trends"]["target_search_trends"]


def test_auto_pick_refuses_flagged_term():
    from trend_scout.tools import save_search_trends_to_session_state

    ctx = _ctx(FLAGS)
    result = save_search_trends_to_session_state("  storm   ADA ", ctx)
    assert result["status"] == "refused"
    assert "sensitive" in result["message"]
    assert _saved(ctx) == []


def test_auto_pick_saves_unflagged_term():
    from trend_scout.tools import save_search_trends_to_session_state

    ctx = _ctx(FLAGS)
    assert save_search_trends_to_session_state("Golden Dip", ctx)["status"] == "ok"
    assert _saved(ctx) == ["Golden Dip"]


def test_flags_as_json_string_and_fenced_json():
    import json

    from trend_scout.tools import save_search_trends_to_session_state

    for raw in (json.dumps(FLAGS), "```json\n" + json.dumps(FLAGS) + "\n```"):
        ctx = _ctx(raw)
        assert save_search_trends_to_session_state("Storm Ada", ctx)["status"] == (
            "refused"
        )
        assert _saved(ctx) == []


def test_interactive_pick_saves_flagged_term():
    from trend_scout.tools import save_search_trends_to_session_state

    ctx = _ctx(FLAGS, interactive=True)
    assert save_search_trends_to_session_state("Storm Ada", ctx)["status"] == "ok"
    assert _saved(ctx) == ["Storm Ada"]


def test_missing_or_malformed_flags_never_block():
    from trend_scout.tools import save_search_trends_to_session_state

    for flags in (None, "", "not json", {"flags": "x"}, [1, 2], {"flags": [None]}):
        ctx = _ctx(flags)
        assert save_search_trends_to_session_state("Storm Ada", ctx)["status"] == "ok"


def test_sensitive_flags_parser():
    from trend_scout.trend_context import sensitive_flags

    assert sensitive_flags(FLAGS) == {"storm ada": "deadly storm"}
    assert sensitive_flags({"flags": [{"term": "X", "sensitive": "true"}]}) == {"x": ""}
    assert sensitive_flags(None) == {}
