"""Sanitize model text output before strict JSON parsing.

Gemini occasionally emits a bare (lone) Unicode surrogate code unit
(U+D800–U+DFFF that is NOT part of a valid high+low pair) inside its JSON text
output — typically from a truncated emoji or hashtag. When an ADK agent sets
`output_schema=`, ADK validates the model's raw text with Pydantic's
`model_validate_json`, which rejects lone surrogates:

    Invalid JSON: lone leading surrogate in hex escape

crashing the ad-copy step of a `creative_agent` run.

`scrub_lone_surrogates` removes ONLY lone surrogates. Valid surrogate PAIRS
(real astral-plane emoji that happen to arrive as two code units) are combined
into their single code point — the pair form itself does not parse as JSON, so
combining is required to preserve, not corrupt, the emoji. `scrub_surrogates_in_response`
is an `after_model_callback` that applies the scrubber to an `LlmResponse`'s text
parts in place BEFORE ADK validates them against the schema. It mutates and
returns `None`, so it composes cleanly with other `after_model_callback`s.

This module imports `google.adk`/`google.genai` types but builds no genai
client, so it stays non-creds-gated and unit-testable offline.
"""

from google.adk.models.llm_response import LlmResponse
from google.adk.agents.callback_context import CallbackContext

_HIGH_MIN, _HIGH_MAX = 0xD800, 0xDBFF
_LOW_MIN, _LOW_MAX = 0xDC00, 0xDFFF


def scrub_lone_surrogates(text: str) -> str:
    """Return ``text`` with lone Unicode surrogates removed.

    A lone surrogate is a code unit in U+D800–U+DFFF that is not part of a valid
    high+low pair. Lone surrogates are dropped; valid pairs are combined into
    their astral code point (which round-trips through JSON); all other
    characters pass through unchanged.
    """
    if not text:
        return text

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        code = ord(text[i])
        if _HIGH_MIN <= code <= _HIGH_MAX:
            # High surrogate: keep only if immediately followed by a low one.
            if i + 1 < n and _LOW_MIN <= ord(text[i + 1]) <= _LOW_MAX:
                low = ord(text[i + 1])
                combined = 0x10000 + ((code - _HIGH_MIN) << 10) + (low - _LOW_MIN)
                out.append(chr(combined))
                i += 2
                continue
            # Lone high surrogate -> drop.
            i += 1
            continue
        if _LOW_MIN <= code <= _LOW_MAX:
            # Lone low surrogate -> drop.
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def scrub_surrogates_in_response(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> None:
    """`after_model_callback` that scrubs lone surrogates from text parts.

    Set on agents that use `output_schema=` so lone surrogates are removed from
    the model's raw text BEFORE ADK validates it with `model_validate_json`.
    Mutates the response in place and returns `None`, so it composes with other
    `after_model_callback`s (e.g. the empty-turn logger).
    """
    # pylint: disable=unused-argument
    if (
        llm_response is None
        or not llm_response.content
        or not llm_response.content.parts
    ):
        return None
    for part in llm_response.content.parts:
        text = getattr(part, "text", None)
        if text:
            cleaned = scrub_lone_surrogates(text)
            if cleaned != text:
                part.text = cleaned
    return None
