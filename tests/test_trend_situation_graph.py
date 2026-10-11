"""Offline runs of the trend situation branch inside combined_research_pipeline
(the trend's validated current moment), driven through the real root with
scripted stub models (helpers from tests/test_creative_agent_graph.py)."""

import json

from tests._fakes import text_response
from tests.test_creative_agent_graph import (  # noqa: F401 (autouse fixture)
    _BRIEF,
    _BoomLlm,
    _fake_research_pdf,
    _grounded,
    _patch_agent_model,
    _responses,
    _run_root,
    _script_research,
    _stub_graph,
)

_NOW_STATE = {
    "current_datetime": "2026-10-11T10:00:00-04:00",
    "current_timezone": "America/New_York",
}
_DRAFT = {
    "phase": "upcoming",
    "event_name": "Roadrunner Cup final",
    "event_start": "2026-10-12",
    "source_ids": ["src-1"],
    "confidence": "high",
}


def test_trend_situation_branch_validates_a_grounded_draft(monkeypatch):
    """With TREND_STATE_ENABLED on, the branch runs in the research fan-out: its
    searcher's grounding joins `sources`, the synthesizer drafts, and the
    validator writes `trend_situation` + `trend_situation_md`."""
    import creative_agent.agent as ca

    monkeypatch.setattr(ca.config, "trend_state_enabled", True)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])
    llms["trend_situation_searcher"].push(_grounded("final is tomorrow, Oct 12"))
    llms["trend_situation_synthesizer"].push(text_response(json.dumps(_DRAFT)))

    root_llm, events, state = _run_root(
        monkeypatch, "combined_research_pipeline", extra_state=_NOW_STATE
    )

    assert llms["trend_situation_searcher"].calls == 1
    assert llms["trend_situation_synthesizer"].calls == 1
    synth_prompt = str(
        llms["trend_situation_synthesizer"].requests[-1].config.system_instruction
    )
    assert "final is tomorrow, Oct 12" in synth_prompt
    assert "src-1" in synth_prompt  # the harvested sources are citable
    assert state["trend_situation_draft"]["phase"] == "upcoming"
    situation = state["trend_situation"]
    assert situation["phase"] == "upcoming"
    assert situation["timely_ok"] is True
    assert situation["valid_until"] == "2026-10-12T23:59:59-04:00"
    assert state["trend_situation_md"].startswith(
        "Current moment (upcoming): Roadrunner Cup final"
    )
    assert state["creative_brief"] == _BRIEF
    assert len(_responses(events)) == 1
    assert root_llm.calls == 2


def test_trend_situation_failure_abstains_and_research_continues(monkeypatch):
    import creative_agent.agent as ca

    monkeypatch.setattr(ca.config, "trend_state_enabled", True)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    boom = _BoomLlm()
    _patch_agent_model(
        monkeypatch, ca.combined_research_pipeline, "trend_situation_synthesizer", boom
    )
    _script_research(llms, ["CA INSIGHTS"])
    llms["trend_situation_searcher"].push(_grounded("raw"))

    root_llm, events, state = _run_root(
        monkeypatch, "combined_research_pipeline", extra_state=_NOW_STATE
    )

    assert boom.requests
    assert state["trend_situation_draft"] is None
    assert state["trend_situation"]["phase"] == "unknown"
    assert state["trend_situation"]["timely_ok"] is False
    assert state["trend_situation_md"] == ""
    assert state["creative_brief"] == _BRIEF
    assert len(_responses(events)) == 1
    assert root_llm.calls == 2


def test_trend_situation_empty_turns_exhaust_to_unknown(monkeypatch):
    import creative_agent.agent as ca

    monkeypatch.setattr(ca.config, "trend_state_enabled", True)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])
    for _ in range(2):
        llms["trend_situation_searcher"].push(text_response("raw"))
        llms["trend_situation_synthesizer"].push(text_response(""))

    _, events, state = _run_root(
        monkeypatch, "combined_research_pipeline", extra_state=_NOW_STATE
    )

    assert llms["trend_situation_synthesizer"].calls == 2
    assert state["trend_situation_draft__retry_exhausted"] is True
    assert state["trend_situation"]["phase"] == "unknown"
    assert len(_responses(events)) == 1


def test_trend_situation_disabled_makes_no_model_call(monkeypatch):
    import creative_agent.agent as ca

    monkeypatch.setattr(ca.config, "trend_state_enabled", False)
    llms = _stub_graph(monkeypatch, ca.combined_research_pipeline)
    _script_research(llms, ["CA INSIGHTS"])

    _, events, state = _run_root(
        monkeypatch,
        "combined_research_pipeline",
        extra_state={**_NOW_STATE, "trend_situation_draft": _DRAFT},
    )

    assert llms["trend_situation_searcher"].calls == 0
    assert llms["trend_situation_synthesizer"].calls == 0
    # A stale draft is cleared, never validated.
    assert state["trend_situation_draft"] is None
    assert state["trend_situation"]["phase"] == "unknown"
    assert state["trend_situation"]["abstain_reason"] == "disabled"
    assert state["trend_situation_md"] == ""
    assert len(_responses(events)) == 1
