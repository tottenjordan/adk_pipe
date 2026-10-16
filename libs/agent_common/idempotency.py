"""Deterministic row keys for idempotent BigQuery writes.

Resumable ADK apps and CRF retries give at-least-once tool execution, so a BQ
write tool may run more than once per session. Deriving the row key from the
session id (never uuid4) and writing with ``MERGE ... WHEN NOT MATCHED THEN
INSERT`` leaves exactly one logical row however many times the tool runs.
"""

import hashlib
import json


def stable_row_id(*parts: str, length: int = 8) -> str:
    """Hex digest prefix of ``parts``; same inputs always give the same id.

    Parts are framed with ``json.dumps`` (not a separator join) so
    ``("a|b", "c")`` and ``("a", "b|c")`` can't collide.
    """
    return hashlib.sha256(json.dumps(list(parts)).encode()).hexdigest()[:length]
