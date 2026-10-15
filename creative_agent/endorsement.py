"""Endorsement wording: a small lexicon for the safer-angle copy check.

Pure logic (no ADK imports). When a run takes a safer angle on a sensitive
trend (the user picked a flagged trend and opted in), the creative may name
the trend's real person only as a respectful tribute: it must never imply that
the person (or their estate) endorses, sponsors or approves the product, and
never speak for them ("would have wanted", "in his words", "RIP"). The copy
gate flags any ad copy field with one of these terms.

Matching mirrors ``claims.py`` (the shared ``lexicon.Lexicon``): whole-word,
case- and accent-insensitive (``text_match.fold``), spaces and hyphens inside a term interchangeable
("signature-model" == "signature model"); "in his own words" counts as
"in his words".

Pinned decisions (``tests/test_endorsement.py``), erring on the safe side:

* Removed first, because they never carry an endorsement sense and are cheap
  to recognise: "partner(s/ed/ing) up" and "partner(s) in crime" (a bandmate,
  not a brand deal) and "real estate". Every other bare "partner" counts
  ("your partner on stage" is flagged; the reviser rewrites it).
* "RIP" is matched case-sensitively on the raw text ("RIP", "R.I.P."), since
  the folded "rip" is a common verb ("let it rip"); an all-caps "LET IT RIP"
  headline is a rare false positive the reviser can fix.
* "approved" alone, "signature" alone and "your favourite" (the reader's) do
  not count; only "approved by", "signature model/edition" and
  "his/her/their favourite" do.
"""

from __future__ import annotations

import re

from .lexicon import Lexicon

ENDORSEMENT_TERMS = (
    "official",
    "officially",
    "endorse",
    "endorsed",
    "endorses",
    "endorsement",
    "sponsor",
    "sponsored",
    "sponsorship",
    "partner",
    "partnered",
    "partnership",
    "approved by",
    "licensed",
    "authorised",
    "authorized",
    "estate",
    "signature model",
    "signature edition",
    "as used by",
    "his favourite",
    "his favorite",
    "her favourite",
    "her favorite",
    "their favourite",
    "their favorite",
    "rest in peace",
    "rip",
    "would have wanted",
    "in his words",
    "in her words",
    "in their words",
)

_LEXICON = Lexicon(
    (t for t in ENDORSEMENT_TERMS if t != "rip"),  # "rip": case-sensitive, below
    idioms=("partner(?:s|ed|ing)? up", "partners? in crime", "real estate"),
    # "in his own words" == "in his words".
    rewrites=((r"\b(in[\s-]+(?:his|her|their))[\s-]+own[\s-]+(words)\b", r"\1 \2"),),
)
_RIP = re.compile(r"(?<![\w.])(?:RIP|R\.I\.P\.?)(?![\w])")


def endorsement_terms(text: str) -> list[str]:
    """The ``ENDORSEMENT_TERMS`` in ``text``, once each, in lexicon order."""
    if not text:
        return []
    found = set(_LEXICON.find(text))
    if _RIP.search(text):
        found.add("rip")
    return [t for t in ENDORSEMENT_TERMS if t in found]
