"""Pure helpers for casting the user's consented person reference.

The person reference (``state["person_reference"] = {uri, consent_id}``) is seeded
via createSession initialState and consent-checked by the api at kick-off
(``runserver.async_runs``). The engine side only re-checks the shape: a ``gs://``
URI under a ``person-refs/`` folder. Person photo URIs are never logged.
"""

from __future__ import annotations

import re
from typing import Any

PERSON_REFS_SEGMENT = "person-refs/"
_PERSON_URI_RE = re.compile(r"^gs://[^/]+/person-refs/[^/]+/[^/]+$")


def person_reference_uri(value: Any) -> str:
    """The person photo's ``gs://`` URI when ``value`` is a ``{uri, ...}`` mapping
    whose URI sits under ``gs://<bucket>/person-refs/<slug>/``; ``""`` otherwise."""
    if not isinstance(value, dict):
        return ""
    uri = value.get("uri")
    if not isinstance(uri, str) or ".." in uri:
        return ""
    uri = uri.strip()
    return uri if _PERSON_URI_RE.match(uri) else ""


def person_reference_available(value: Any) -> str:
    """``"yes"`` when ``value`` names a usable person reference, else ``""`` (the
    visual agents read it as ``{person_reference_available?}``; never the URI)."""
    return "yes" if person_reference_uri(value) else ""
