"""Tests for the lone-surrogate scrubber in `agent_common.sanitize`.

Gemini occasionally emits a bare (lone) Unicode surrogate code unit
(U+D800–U+DFFF not part of a high+low pair) inside its JSON text output — e.g.
a truncated emoji or hashtag. Pydantic's `model_validate_json` (used by ADK when
an agent sets `output_schema=`) rejects lone surrogates with
"Invalid JSON: lone leading surrogate in hex escape", crashing an ad-copy run.

These lock in that:
- `scrub_lone_surrogates` drops ONLY lone surrogates and leaves valid content
  intact (including real astral-plane emoji, which must round-trip through JSON).
- The `scrub_surrogates_in_response` `after_model_callback` cleans an
  `LlmResponse`'s text parts in place BEFORE ADK validates them against the
  schema, without disturbing the happy path.
"""

from types import SimpleNamespace

from google.genai import types
from google.adk.models.llm_response import LlmResponse
from pydantic import BaseModel

from agent_common import sanitize


class _Copy(BaseModel):
    headline: str


class _CopyList(BaseModel):
    ad_copies: list[_Copy]


def _ctx():
    return SimpleNamespace(agent_name="ad_copy_drafter", invocation_id="inv-1")


def _resp(*, parts=None):
    content = types.Content(role="model", parts=parts) if parts is not None else None
    return LlmResponse(content=content)


# --- scrub_lone_surrogates --------------------------------------------------


def test_lone_high_surrogate_scrubbed_output_parses():
    """The exact reported crash payload: a lone high surrogate in JSON text."""
    payload = '{"ad_copies": [{"headline": "Buy now \ud83d"}]}'
    cleaned = sanitize.scrub_lone_surrogates(payload)
    # Scrubbed text must now validate against the schema (no lone surrogate).
    parsed = _CopyList.model_validate_json(cleaned)
    assert parsed.ad_copies[0].headline == "Buy now "


def test_lone_low_surrogate_scrubbed():
    payload = '{"ad_copies": [{"headline": "x' + chr(0xDE00) + 'y"}]}'
    cleaned = sanitize.scrub_lone_surrogates(payload)
    parsed = _CopyList.model_validate_json(cleaned)
    assert parsed.ad_copies[0].headline == "xy"


def test_valid_surrogate_pair_preserved_as_real_emoji():
    """A valid high+low pair (two code units) is real astral content and must NOT
    be corrupted — it is combined into its code point and still parses as JSON."""
    hi, lo = chr(0xD83D), chr(0xDE00)  # -> 😀 (U+1F600)
    payload = '{"ad_copies": [{"headline": "grin ' + hi + lo + '"}]}'
    cleaned = sanitize.scrub_lone_surrogates(payload)
    parsed = _CopyList.model_validate_json(cleaned)
    assert parsed.ad_copies[0].headline == "grin \U0001f600"


def test_real_astral_emoji_untouched():
    """A real emoji arriving already as a single code point passes through."""
    payload = '{"ad_copies": [{"headline": "party \U0001f389"}]}'
    assert sanitize.scrub_lone_surrogates(payload) == payload


def test_plain_text_unchanged():
    text = "no surrogates here #trending"
    assert sanitize.scrub_lone_surrogates(text) == text


def test_empty_string():
    assert sanitize.scrub_lone_surrogates("") == ""


# --- scrub_surrogates_in_response (after_model_callback) ---------------------


def test_callback_cleans_lone_surrogate_in_response_text():
    resp = _resp(parts=[types.Part(text='{"headline": "Buy \ud83d"}')])
    ret = sanitize.scrub_surrogates_in_response(
        callback_context=_ctx(), llm_response=resp
    )
    # Mutates in place and returns None so it composes with other callbacks.
    assert ret is None
    cleaned = resp.content.parts[0].text
    assert cleaned == '{"headline": "Buy "}'
    # And the scrubbed text is valid JSON again.
    _Copy.model_validate_json(cleaned)


def test_callback_leaves_clean_text_untouched():
    resp = _resp(parts=[types.Part(text='{"headline": "clean"}')])
    sanitize.scrub_surrogates_in_response(callback_context=_ctx(), llm_response=resp)
    assert resp.content.parts[0].text == '{"headline": "clean"}'


def test_callback_handles_response_without_content():
    resp = _resp(parts=None)
    assert (
        sanitize.scrub_surrogates_in_response(
            callback_context=_ctx(), llm_response=resp
        )
        is None
    )


def test_callback_ignores_none_and_non_text_parts():
    resp = _resp(
        parts=[
            types.Part(function_call=types.FunctionCall(name="x", args={})),
            types.Part(text="ok \ud83d"),
        ]
    )
    sanitize.scrub_surrogates_in_response(callback_context=_ctx(), llm_response=resp)
    assert resp.content.parts[1].text == "ok "
