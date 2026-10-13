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


def pretty_print_event(event):
    """Pretty prints an event with truncation for long content."""
    if not event.get("content"):
        logging.info(f"[{event.get('author', 'unknown')}]: {event}")
        return

    author = event.get("author", "unknown")
    parts = event["content"].get("parts") or []

    for part in parts:
        # Deployed AdkApp engines stream snake_case dicts (nulls dropped); accept
        # camelCase too. Inline mirror of deployment/test_deployment.py (the CRF
        # package can't import deployment/).
        func_call = part.get("function_call") or part.get("functionCall")
        func_response = part.get("function_response") or part.get("functionResponse")
        if part.get("text"):
            text = part["text"]
            logging.info(f"[{author}]: {text}")
        elif func_call:
            logging.info(
                f"[{author}]: Function call: {func_call.get('name', 'unknown')}"
            )
            # Truncate args if too long
            args = json.dumps(func_call.get("args", {}))
            if len(args) > 100:
                args = args[:97] + "..."
            logging.info(f"  Args: {args}")
        elif func_response:
            logging.info(
                f"[{author}]: Function response: {func_response.get('name', 'unknown')}"
            )
            # Truncate response if too long
            response = json.dumps(func_response.get("response", {}))
            if len(response) > 100:
                response = response[:97] + "..."
            logging.info(f"  Response: {response}")
