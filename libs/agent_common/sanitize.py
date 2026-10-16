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

`build_invalid_reply_dropper(schema)` builds an `after_model_callback` for an
OPTIONAL structured producer under a `RetryUntilKeyNode`: a reply that still
fails the schema is blanked in place, so it degrades like an empty reply
instead of raising (see its docstring for the ADK source it mirrors).

This module imports `google.adk`/`google.genai` types but builds no genai
client, so it stays non-creds-gated and unit-testable offline.
"""

import logging
from collections.abc import Callable
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse

# The exact validator ADK applies to an output_schema agent's final text
# (google/adk/agents/llm_agent.py `__maybe_save_output_to_state` →
# google/adk/utils/_schema_utils.py `validate_schema`: strips a JSON code fence,
# then `model_validate_json`). Private, so imported where it is used.
from google.adk.utils._schema_utils import validate_schema

logger = logging.getLogger(__name__)

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


def build_invalid_reply_dropper(
    schema: Any,
) -> Callable[[CallbackContext, LlmResponse], None]:
    """An `after_model_callback` that blanks a reply failing ``schema``.

    In ADK 2.10 an output_schema agent's final text is validated AFTER the
    after-model callbacks, in ``LlmAgent.__maybe_save_output_to_state``
    (google/adk/agents/llm_agent.py), with ``validate_schema``
    (google/adk/utils/_schema_utils.py). A failure raises a pydantic
    ``ValidationError`` out of the agent run, which ``RetryUntilKeyNode`` does
    not treat as a missed attempt (only empty output and transient errors), so
    one bad reply would fail the whole run. A blank/whitespace final text, by
    contrast, is skipped there ("empty final chunk": the output key stays
    unset), which the wrapper retries and finally records as
    ``<key>__retry_exhausted``.

    This callback runs the same ``validate_schema`` on the joined non-thought
    text parts; on failure it logs a warning and sets those parts' text to ""
    in place (returns ``None`` so it composes with the other callbacks). Put it
    after ``scrub_surrogates_in_response`` so a scrub-able reply still passes.
    """

    name = getattr(schema, "__name__", str(schema))

    def drop_invalid_reply(
        callback_context: CallbackContext, llm_response: LlmResponse
    ) -> None:
        # pylint: disable=unused-argument
        if (
            llm_response is None
            or not llm_response.content
            or not llm_response.content.parts
        ):
            return None
        parts = [
            p
            for p in llm_response.content.parts
            if getattr(p, "text", None) and not getattr(p, "thought", False)
        ]
        text = "".join(p.text or "" for p in parts)
        if not text.strip():
            return None
        try:
            validate_schema(schema, text)
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            logger.warning(
                "%s reply does not match %s (%s); dropped as an empty reply",
                callback_context.agent_name,
                name,
                type(exc).__name__,
            )
            for part in parts:
                part.text = ""
        return None

    return drop_invalid_reply
