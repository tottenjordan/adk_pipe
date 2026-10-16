"""Structured Cloud Run logging for the CRF functions.

Importing this module configures the root logger (JSON lines on stdout) once
per process; every other CRF module imports it first, via ``main``.
"""

import json
import logging
import os
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

# Cloud Logging's trace-correlation keys in a JSON log line
# (https://docs.cloud.google.com/run/docs/logging): a container line carrying
# the request's trace nests under that request's log entry.
TRACE_KEY = "logging.googleapis.com/trace"
SPAN_KEY = "logging.googleapis.com/spanId"
SAMPLED_KEY = "logging.googleapis.com/trace_sampled"

# The current request's trace keys, set by ``bind_trace`` at each entry point.
# ``asyncio.run`` copies the context, so the worker's ``process_row`` logs
# carry them too.
_trace: ContextVar[dict | None] = ContextVar("crf_trace", default=None)

# X-Cloud-Trace-Context: TRACE_ID/SPAN_ID;o=OPTIONS (SPAN_ID decimal; span and
# options optional); traceparent: VERSION-TRACE_ID-SPAN_ID-FLAGS (W3C, all
# hex) (https://docs.cloud.google.com/trace/docs/trace-context).
_CLOUD_TRACE = re.compile(r"^([0-9a-f]{32})(?:/(\d+))?(?:;o=(\d+))?$")
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def _parse_trace(headers) -> tuple[str, str | None, bool] | None:
    """``(trace_id, span_hex, sampled)`` from ``X-Cloud-Trace-Context``, else
    ``traceparent``; None when neither parses (or the trace id is all zeros).

    Cloud Logging's ``spanId`` is 16 hex digits and never zero (LogEntry
    reference), so X-Cloud-Trace-Context's decimal SPAN_ID is converted to hex
    (dropped when 0 or past 64 bits); traceparent's is hex already.
    """
    cloud = _CLOUD_TRACE.match((headers.get("X-Cloud-Trace-Context") or "").strip())
    if cloud:
        trace_id, span, options = cloud.groups()
        span_int = int(span) if span else 0
        span_hex = f"{span_int:016x}" if 0 < span_int < 2**64 else None
        sampled = options == "1"
    else:
        parent = _TRACEPARENT.match((headers.get("traceparent") or "").strip())
        if not parent:
            return None
        trace_id, span_hex, flags = parent.groups()
        span_hex = span_hex if int(span_hex, 16) else None
        sampled = bool(int(flags, 16) & 1)
    return (trace_id, span_hex, sampled) if int(trace_id, 16) else None


def bind_trace(headers) -> None:
    """Bind the request's trace (``headers``: any mapping with ``.get``) to
    this context, so every log line until the next call carries it as
    ``projects/{GOOGLE_CLOUD_PROJECT}/traces/{TRACE_ID}`` (+ ``spanId`` and
    ``trace_sampled``). No usable header, or no project, clears the binding,
    so a reused thread never logs a previous request's trace."""
    parsed = _parse_trace(headers)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if parsed is None or not project:
        _trace.set(None)
        return
    trace_id, span_hex, sampled = parsed
    fields: dict = {TRACE_KEY: f"projects/{project}/traces/{trace_id}"}
    if span_hex:
        fields[SPAN_KEY] = span_hex
    fields[SAMPLED_KEY] = sampled
    _trace.set(fields)


class CloudRunJsonFormatter(logging.Formatter):
    """One JSON object per line with ``severity`` + ``message`` (+ ``time``).

    Cloud Run parses JSON lines on stdout/stderr into structured log entries:
    ``severity`` becomes the entry's level and ``message`` its text
    (https://docs.cloud.google.com/run/docs/logging). A record's optional
    ``json_fields`` dict (``logger.info(..., extra={"json_fields": {...}})``)
    is merged in as extra top-level keys (queryable as ``jsonPayload.<key>``);
    it never overrides ``severity`` / ``message`` / ``time`` or the trace keys
    ``bind_trace`` bound for the current request.
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
        entry.update(_trace.get() or {})
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


# force=True replaces any root handlers already there: with LOG_EXECUTION_ID
# set, functions-framework configures the root logger at WARNING before it
# imports `main`, and a plain basicConfig would then do nothing (INFO lost).
logging.basicConfig(level=logging.INFO, handlers=[_json_log_handler()], force=True)

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
