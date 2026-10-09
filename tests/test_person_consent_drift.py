"""The consent text + version shown on the /people page match the api's.

The api refuses a registration whose ``consent_text_version`` isn't current, and
the record stores only the version, so the frontend copy
(``frontend/src/lib/person-consent.ts``) must be exactly the wording the version
names (``runserver/person_refs.py``)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from runserver.person_refs import CONSENT_TEXT, CONSENT_TEXT_VERSION

TS = Path(__file__).resolve().parents[1] / "frontend/src/lib/person-consent.ts"


def _ts_string(name: str) -> str:
    m = re.search(rf'export const {name} = ("(?:[^"\\]|\\.)*");', TS.read_text())
    assert m, f"{name} must be a single double-quoted string literal in {TS.name}"
    return json.loads(m.group(1))


def test_consent_version_matches():
    assert _ts_string("CONSENT_TEXT_VERSION") == CONSENT_TEXT_VERSION


def test_consent_text_matches():
    assert _ts_string("CONSENT_TEXT") == CONSENT_TEXT


def test_version_is_a_date():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", CONSENT_TEXT_VERSION)
