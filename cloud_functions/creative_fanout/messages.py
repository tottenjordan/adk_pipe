"""Pub/Sub message parsing for the CRF functions.

Decodes and validates the orchestrator's trigger message and the worker
message, and builds the worker message's ``row_data`` from a BigQuery row.
"""

import base64
import json
import logging
from datetime import date, datetime

from .config import config
from .status_store import _to_utc_datetime, _validate_target


class MalformedMessageError(ValueError):
    """A Pub/Sub message that can never be processed, however often it is
    redelivered: the entry points log it as an error and ACK (return) instead
    of raising, which would NACK it into a redelivery loop."""


# The engine is the worker's CREATIVE_AGENT_ENGINE_ID env var; a legacy
# `agent_resource_id` in a message is ignored (`_note_legacy_engine_id`).
_TRIGGER_FIELDS = ("bq_dataset", "bq_table")
_WORKER_FIELDS = (*_TRIGGER_FIELDS, "row_data")
# Every row_data key the worker indexes (build_row_dict always sends them; a
# campaign value may be null).
_ROW_FIELDS = (
    "index",
    "entry_timestamp",
    "brand",
    "target_product",
    "key_selling_point",
    "target_audience",
    "target_search_trend",
)


def _decode_pubsub_json(cloud_event) -> dict:
    """The CloudEvent's base64 Pub/Sub ``message.data`` as a JSON object.

    Raises MalformedMessageError for anything else (no data, bad base64 or
    UTF-8, invalid JSON, a non-object payload).
    """
    try:
        raw = base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
        payload = json.loads(raw)
    except (KeyError, TypeError, ValueError) as e:
        # binascii.Error, UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise MalformedMessageError(
            f"undecodable Pub/Sub message ({type(e).__name__}: {e})"
        ) from e
    if not isinstance(payload, dict):
        raise MalformedMessageError(
            f"payload is a JSON {type(payload).__name__}, not an object"
        )
    return payload


def _require_fields(mapping: dict, fields, what: str) -> None:
    missing = [f for f in fields if f not in mapping]
    if missing:
        raise MalformedMessageError(f"{what} missing {', '.join(missing)}")


def _note_legacy_engine_id(payload: dict) -> None:
    """Warn when a message still names an engine other than the worker's.

    The engine comes from ``config.CREATIVE_AGENT_ENGINE_ID`` (the worker's
    env); a legacy ``agent_resource_id`` (an old trigger, or a worker message
    in flight across the deploy) is never used, whatever its value.
    """
    if "agent_resource_id" not in payload:
        return
    legacy = payload["agent_resource_id"]
    if legacy != config.CREATIVE_AGENT_ENGINE_ID:
        logging.warning(
            f"Ignoring agent_resource_id={legacy!r} in the message: the worker "
            "runs the engine in its CREATIVE_AGENT_ENGINE_ID env var "
            f"({config.CREATIVE_AGENT_ENGINE_ID or 'unset'!r})."
        )


def _validate_row_key(row_data: dict) -> None:
    """``row_data``'s optional row key: no ``uuid`` (a legacy message, or a row
    written without one: matched by entry_timestamp) or a ``uuid`` and a
    ``target_trend`` that are both non-empty strings."""
    key = (row_data.get("uuid"), row_data.get("target_trend"))
    if key[0] is None:
        return
    if not all(isinstance(v, str) and v.strip() for v in key):
        raise MalformedMessageError(
            f"invalid row key uuid={key[0]!r} target_trend={key[1]!r}"
        )


def parse_trigger_message(cloud_event) -> dict | None:
    """The orchestrator trigger's validated payload, or None when it is not work.

    A trigger without data or without ``bq_dataset`` is not work: it is logged
    and None returned (the caller ACKs). Raises ValueError
    (MalformedMessageError, or the identifier validator's ValueError) for a
    message that can never be processed: the dataset/table become SQL
    identifiers, so they must be on the config allow-list, and a missing
    field or a bad identifier can never become valid on redelivery. A legacy
    ``agent_resource_id`` is ignored (a warning when it differs from the
    worker's engine).
    """
    data = cloud_event.data
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict) or "data" not in message:
        logging.info("No data found in the Pub/Sub message.")
        return None
    message_payload = _decode_pubsub_json(cloud_event)
    logging.info(f"Parsed message payload: {message_payload}")
    if "bq_dataset" not in message_payload:
        logging.info("Trigger message has no 'bq_dataset'; nothing to process.")
        return None

    _require_fields(message_payload, _TRIGGER_FIELDS, "trigger message")
    _note_legacy_engine_id(message_payload)
    _validate_target(message_payload["bq_dataset"], message_payload["bq_table"])
    return message_payload


def parse_worker_message(cloud_event) -> tuple[str, str, dict]:
    """``(dataset, table, row_data)`` from a worker message.

    A legacy ``agent_resource_id`` (a message published before the engine
    moved to the worker's env) is ignored.

    Raises ValueError (MalformedMessageError, or the identifier / timestamp
    validators' ValueError) for a message that can never be processed.
    """
    payload = _decode_pubsub_json(cloud_event)
    _require_fields(payload, _WORKER_FIELDS, "worker message")
    row_data = payload["row_data"]
    if not isinstance(row_data, dict):
        raise MalformedMessageError("row_data is not an object")
    _require_fields(row_data, _ROW_FIELDS, "row_data")
    index = row_data["index"]
    if isinstance(index, bool) or not isinstance(index, int):
        raise MalformedMessageError(f"row_data.index {index!r} is not an integer")
    _note_legacy_engine_id(payload)
    # dataset/table stay in the worker message (compatible with messages already
    # in flight across a deploy) but must be allow-listed; the timestamp must
    # parse (the lock builder would raise on it otherwise).
    _validate_target(payload["bq_dataset"], payload["bq_table"])
    _to_utc_datetime(row_data["entry_timestamp"])
    _validate_row_key(row_data)
    return payload["bq_dataset"], payload["bq_table"], row_data


def _optional_cell(value) -> str:
    """A nullable BigQuery cell as a JSON-safe string ("" for NULL).

    DATE/TIMESTAMP cells become an ISO date (``YYYY-MM-DD``).
    """
    if value is None:
        return ""
    # datetime before date: a datetime IS a date, and its time part is dropped.
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def build_row_dict(index, row) -> dict:
    """The worker message's ``row_data`` for one BigQuery row (JSON-safe).

    ``row`` is a BigQuery ``Row`` (or mapping); a NULL campaign string stays
    ``None`` (JSON null). ``refresh_date`` / ``trend_context``
    are read with ``.get`` so a table without the trend_context column (before
    its ALTER) still dispatches.
    """
    return {
        "index": index,
        # The row key (trend_scout's MERGE key): every worker statement matches it.
        "uuid": row.get("uuid"),
        "target_trend": row["target_trend"],
        # ISO 8601 string for JSON serialization (ordering and logs only)
        "entry_timestamp": row["entry_timestamp"].isoformat(),
        "target_search_trend": row["target_trend"],
        "brand": row["brand"],
        "target_audience": row["target_audience"],
        "target_product": row["target_product"],
        "key_selling_point": row["key_selling_point"],
        "refresh_date": _optional_cell(row.get("refresh_date")),
        "trend_context": _optional_cell(row.get("trend_context")),
    }
