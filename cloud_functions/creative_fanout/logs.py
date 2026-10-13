"""Structured Cloud Run logging for the CRF functions.

Importing this module configures the root logger (JSON lines on stdout) once
per process; every other CRF module imports it first, via ``main``.
"""

import json
import logging
import sys
from datetime import UTC, datetime


class CloudRunJsonFormatter(logging.Formatter):
    """One JSON object per line with ``severity`` + ``message`` (+ ``time``).

    Cloud Run parses JSON lines on stdout/stderr into structured log entries:
    ``severity`` becomes the entry's level and ``message`` its text
    (https://docs.cloud.google.com/run/docs/logging). A record's optional
    ``json_fields`` dict (``logger.info(..., extra={"json_fields": {...}})``)
    is merged in as extra top-level keys (queryable as ``jsonPayload.<key>``);
    it never overrides ``severity`` / ``message`` / ``time``.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"
        entry = {}
        fields = getattr(record, "json_fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        entry.update(
            severity=record.levelname,
            message=message,
            time=datetime.fromtimestamp(record.created, UTC).isoformat(),
            logger=record.name,
        )
        return json.dumps(entry, default=str)


def _json_log_handler() -> logging.Handler:
    """A stdout handler writing ``CloudRunJsonFormatter`` lines."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudRunJsonFormatter())
    return handler


# Like the old text basicConfig: a no-op if the root logger already has
# handlers (functions-framework adds none in this image at import time).
logging.basicConfig(level=logging.INFO, handlers=[_json_log_handler()])

logger = logging.getLogger(__name__)

# Caps on what one streamed event may put in the logs (agent text, tool args /
# responses, state-key names); never a whole event or its state_delta.
MAX_TEXT_CHARS = 300
MAX_TOOL_CHARS = 100
MAX_STATE_KEYS = 10


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _event_log(author: str, kind: str, message: str, **fields) -> None:
    """One INFO line per event part, with ``author`` / ``kind`` as JSON keys."""
    logger.info(
        f"[{author}]: {message}",
        extra={"json_fields": {"event_author": author, "event_kind": kind, **fields}},
    )


def pretty_print_event(event) -> None:
    """Log one streamed agent event compactly.

    Text parts are cut to ``MAX_TEXT_CHARS``, function args/responses to
    ``MAX_TOOL_CHARS``; an event without content logs only the names of its
    ``state_delta`` keys (at most ``MAX_STATE_KEYS``), never the event or the
    values (they hold the brief and research report, multi-KB entries).
    """
    if not isinstance(event, dict):
        _event_log("unknown", "unknown", f"event of type {type(event).__name__}")
        return
    author = str(event.get("author") or "unknown")
    content = event.get("content")
    if not content:
        actions = event.get("actions")
        delta = (
            actions.get("state_delta", actions.get("stateDelta"))
            if isinstance(actions, dict)
            else None
        )
        keys = sorted(str(k) for k in delta) if isinstance(delta, dict) else []
        shown = keys[:MAX_STATE_KEYS]
        more = f" (+{len(keys) - len(shown)} more)" if len(keys) > len(shown) else ""
        _event_log(
            author,
            "no_content",
            f"event without content; state keys: {', '.join(shown) or 'none'}{more}",
            state_keys=shown,
        )
        return

    parts = content.get("parts") if isinstance(content, dict) else None
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        # Deployed AdkApp engines stream snake_case dicts (nulls dropped); accept
        # camelCase too. Inline mirror of deployment/test_deployment.py (the CRF
        # package can't import deployment/).
        func_call = part.get("function_call") or part.get("functionCall")
        func_response = part.get("function_response") or part.get("functionResponse")
        if part.get("text"):
            text = str(part["text"])
            _event_log(
                author, "text", _truncate(text, MAX_TEXT_CHARS), text_chars=len(text)
            )
        elif func_call:
            args = _truncate(json.dumps(func_call.get("args", {})), MAX_TOOL_CHARS)
            _event_log(
                author,
                "function_call",
                f"Function call: {func_call.get('name', 'unknown')}  Args: {args}",
            )
        elif func_response:
            response = _truncate(
                json.dumps(func_response.get("response", {}), default=str),
                MAX_TOOL_CHARS,
            )
            _event_log(
                author,
                "function_response",
                f"Function response: {func_response.get('name', 'unknown')}  "
                f"Response: {response}",
            )
