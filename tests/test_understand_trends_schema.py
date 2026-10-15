"""TS-6: understand_trends_synthesizer's `output_schema` (AnalyzedTrends).

The validated dict ADK stores in `info_gtrends` must keep the JSON shape every
reader parses: trend_context (the BigQuery `trend_context` column), the
picker's `{info_gtrends?}` and the frontend trend cards (`analyzed_trends[]`
items with `term` / `context` / `cultural_angle` / `event_hint`).
"""

import json

import pytest
from google.adk.utils._schema_utils import validate_schema
from pydantic import ValidationError

from trend_scout.schemas import AnalyzedTrends
from trend_scout.trend_context import trend_context_for

# A reply in the exact format UNDERSTAND_TRENDS_SYNTHESIZER_INSTR asks for.
FULL_ITEM = {
    "term": "Texas vs Oklahoma",
    "category": "Sports",
    "context": "The Red River rivalry game is this weekend.",
    "cultural_angle": "a century-old rivalry ritual",
    "as_of": "2026-10-09",
    "event_hint": "Texas vs Oklahoma on 2026-10-11",
}
REPLY = json.dumps({"analyzed_trends": [FULL_ITEM, {"term": "harvest moon"}]})


def _stored(reply: str) -> dict:
    """What ADK writes to state[output_key] for a schema'd agent."""
    return validate_schema(AnalyzedTrends, reply)


def test_validated_dict_keeps_the_analyzed_trends_shape():
    stored = _stored(REPLY)
    assert isinstance(stored, dict)
    items = stored["analyzed_trends"]
    assert items[0] == FULL_ITEM
    # Sparse findings validate; missing fields read as "".
    assert items[1] == {
        "term": "harvest moon",
        "category": "",
        "context": "",
        "cultural_angle": "",
        "as_of": "",
        "event_hint": "",
    }
    # JSON round trip (session API → frontend) keeps the same object.
    assert json.loads(json.dumps(stored)) == stored


def test_trend_context_still_parses_the_stored_dict():
    stored = _stored(REPLY)
    got = json.loads(trend_context_for("texas vs oklahoma", stored, ""))
    assert got == {
        "context": "The Red River rivalry game is this weekend.",
        "cultural_angle": "a century-old rivalry ritual",
        "event_hint": "Texas vs Oklahoma on 2026-10-11",
    }
    # ...exactly as the pre-schema JSON-string form parsed.
    assert trend_context_for("texas vs oklahoma", REPLY, "") == trend_context_for(
        "texas vs oklahoma", stored, ""
    )
    # A sparse item contributes nothing (blank fields dropped).
    assert trend_context_for("harvest moon", stored, "") == ""


def test_malformed_reply_fails_the_schema():
    """ADK's own validation rejects a reply that isn't the briefing (instead of
    storing a string trend_context silently drops); the dropper below blanks it
    before ADK would raise."""
    for bad in ('{"analyzed_trends": ["t1"]}', "not json"):
        with pytest.raises(ValidationError):
            _stored(bad)


def _response(*texts: str, thought: bool = False):
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    parts = [types.Part(text=t, thought=thought or None) for t in texts]
    return LlmResponse(content=types.Content(role="model", parts=parts))


class _Ctx:
    agent_name = "understand_trends_synthesizer"


def test_dropper_blanks_only_a_reply_that_fails_the_schema(caplog):
    from agent_common import build_invalid_reply_dropper

    drop = build_invalid_reply_dropper(AnalyzedTrends)
    bad = _response('{"analyzed_trends": ', '["t1"]}')
    with caplog.at_level("WARNING", logger="agent_common.sanitize"):
        assert drop(_Ctx(), bad) is None  # type: ignore[arg-type]
    assert bad.content is not None and bad.content.parts is not None
    assert [p.text for p in bad.content.parts] == ["", ""]
    assert any("AnalyzedTrends" in r.getMessage() for r in caplog.records)

    fenced = "```json\n" + REPLY + "\n```"  # ADK strips the fence too
    for text, thought in ((fenced, False), ("   ", False), ("musing", True)):
        kept = _response(text, thought=thought)
        drop(_Ctx(), kept)  # type: ignore[arg-type]
        assert kept.content is not None and kept.content.parts is not None
        assert kept.content.parts[0].text == text


def test_synthesizer_is_wired_with_schema_scrub_dropper_and_tuned_temperature():
    from agent_common import scrub_surrogates_in_response
    from trend_scout.agent import understand_trends_synthesizer as synth

    assert synth.output_schema is AnalyzedTrends
    assert synth.output_key == "info_gtrends"
    callbacks = synth.after_model_callback
    assert isinstance(callbacks, list) and len(callbacks) == 3
    assert callbacks[0] is scrub_surrogates_in_response
    assert getattr(callbacks[1], "__name__", "") == "drop_invalid_reply"
    assert synth.generate_content_config is not None
    assert synth.generate_content_config.temperature == 1.5
