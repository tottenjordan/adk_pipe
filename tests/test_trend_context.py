"""trend_scout/trend_context.py: the compact per-trend context persisted to
`target_trends_crf.trend_context` and forwarded to creative runs."""

import json

from trend_scout.trend_context import trend_context_for

INFO = {
    "analyzed_trends": [
        {
            "term": "Texas Football",
            "category": "Sports",
            "context": "Texas plays Oklahoma in the Red River Rivalry.",
            "cultural_angle": "rivalry weekend rituals",
            "as_of": "2026-10-11",
            "event_hint": "Texas vs Oklahoma on 2026-10-12",
        },
        {
            "term": "Golden Dip",
            "category": "Food",
            "context": "A viral dessert.",
            "cultural_angle": "nostalgia",
        },
    ]
}

SELECTED = """## Selected Trends Strategy

### Texas Football
* **The "Hook":** Game day, your way
* **Context:** The Red River Rivalry is this weekend.
* **Why it fits:** Fans gather.
* **The Strategic Bridge:** Tailgate tone.

### Golden Dip
* **The "Hook":** Dip into gold
* **Context:** A dessert trend.
"""


def _decode(raw: str) -> dict:
    return json.loads(raw)


def test_json_and_markdown_are_merged():
    out = _decode(trend_context_for("Texas Football", json.dumps(INFO), SELECTED))
    assert out == {
        "context": "Texas plays Oklahoma in the Red River Rivalry.",
        "cultural_angle": "rivalry weekend rituals",
        "event_hint": "Texas vs Oklahoma on 2026-10-12",
        "hook": "Game day, your way",
    }


def test_term_match_is_case_and_space_insensitive():
    out = _decode(trend_context_for("  golden dip ", INFO, SELECTED))
    assert out["context"] == "A viral dessert."
    assert out["hook"] == "Dip into gold"
    assert "event_hint" not in out  # blanks are dropped


def test_fenced_json_string_parses():
    fenced = "```json\n" + json.dumps(INFO) + "\n```"
    out = _decode(trend_context_for("Texas Football", fenced, ""))
    assert out["event_hint"] == "Texas vs Oklahoma on 2026-10-12"
    assert "hook" not in out


def test_markdown_context_is_the_fallback():
    out = _decode(trend_context_for("Golden Dip", "not json", SELECTED))
    assert out == {"context": "A dessert trend.", "hook": "Dip into gold"}


def test_markdown_heading_variants():
    md = '### "Texas Football"\n* **The Hook:** Big game\n* **Context:** Rivalry.\n'
    out = _decode(trend_context_for("Texas Football", None, md))
    assert out == {"context": "Rivalry.", "hook": "Big game"}


def test_section_stops_at_the_next_heading():
    out = _decode(trend_context_for("Texas Football", None, SELECTED))
    assert out["hook"] == "Game day, your way"


def test_malformed_input_gives_empty_string():
    assert trend_context_for("Texas Football", "{not json", "no headings here") == ""
    assert trend_context_for("Texas Football", None, None) == ""
    assert trend_context_for("Texas Football", {"analyzed_trends": "x"}, 42) == ""
    assert trend_context_for("", json.dumps(INFO), SELECTED) == ""


def test_unknown_term_gives_empty_string():
    assert trend_context_for("Unrelated", json.dumps(INFO), SELECTED) == ""


def test_values_are_capped_and_whitespace_collapsed():
    info = {
        "analyzed_trends": [
            {"term": "T", "context": "x" * 2000, "cultural_angle": "a\n  b"}
        ]
    }
    out = _decode(trend_context_for("T", info, ""))
    assert len(out["context"]) <= 400
    assert out["cultural_angle"] == "a b"


def test_non_string_fields_are_ignored():
    info = {"analyzed_trends": [{"term": "T", "context": 7, "cultural_angle": "ok"}]}
    assert _decode(trend_context_for("T", info, "")) == {"cultural_angle": "ok"}


# --- sensitive flag (state trend_flags) ---------------------------------------

FLAGS = {
    "flags": [
        {"term": "golden dip", "sensitive": True, "reason": "  food recall   deaths "},
        {"term": "Texas Football", "sensitive": False, "reason": ""},
    ]
}


def test_flagged_term_adds_sensitive_keys_last():
    raw = trend_context_for("Golden Dip", INFO, SELECTED, flags=FLAGS)
    out = _decode(raw)
    assert list(out) == [
        "context",
        "cultural_angle",
        "hook",
        "sensitive",
        "sensitive_reason",
    ]
    assert out["sensitive"] is True
    assert out["sensitive_reason"] == "food recall deaths"


def test_unflagged_output_is_byte_identical():
    import json as _json

    for flags in (None, FLAGS, _json.dumps(FLAGS), "garbage"):
        assert trend_context_for(
            "Texas Football", INFO, SELECTED, flags=flags
        ) == trend_context_for("Texas Football", INFO, SELECTED)


def test_flag_accepts_json_string_and_caps_reason():
    import json as _json

    from trend_scout.trend_context import MAX_FIELD_CHARS

    flags = {"flags": [{"term": "Golden Dip", "sensitive": True, "reason": "r" * 999}]}
    fenced = "```json\n" + _json.dumps(flags) + "\n```"
    out = _decode(trend_context_for("golden  dip", INFO, SELECTED, flags=fenced))
    assert out["sensitive"] is True
    assert len(out["sensitive_reason"]) == MAX_FIELD_CHARS


def test_flagged_term_without_research_still_carries_the_flag():
    out = _decode(
        trend_context_for(
            "Storm Ada",
            None,
            None,
            flags={
                "flags": [
                    {"term": "Storm Ada", "sensitive": True, "reason": "deadly storm"}
                ]
            },
        )
    )
    assert out == {"sensitive": True, "sensitive_reason": "deadly storm"}
