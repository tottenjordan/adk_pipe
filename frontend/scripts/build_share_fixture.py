"""Derive the share + personalise screenshot fixtures from the PRS run fixtures.

Runs the REAL backend code on ``screenshot-fixtures/creative-state.current.json`` and
``creative-eval-report.current.json`` so the mocked responses have the api's exact shapes:

- ``share-fixture.json``: ``{"snapshot": <runserver.share_snapshot.build_snapshot
  v1 slate with include_eval>, "images": [<fixture image per creative index>]}``,
  served by ``share-gcs-mock.mjs`` (the public ``/s/<token>`` page reads GCS server
  side, so it can't be route-mocked from Playwright).
- ``variants-listing.json``: the ``GET /variants/{user}/{app}/{session}`` body
  (no previews; ``castable`` from ``runserver.variants.castability`` with the
  default person-safe styles) for the Personalise panel.

No person is cast in the fixtures (no person_reference), so nothing here touches
a real person's photo.

Regenerate (from the repo root):
    GOOGLE_CLOUD_PROJECT=test-project uv run python frontend/scripts/build_share_fixture.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

from creative_agent.config import DEFAULT_PERSON_SAFE_STYLES  # noqa: E402
from runserver.share_snapshot import build_snapshot  # noqa: E402
from runserver.variants import castability  # noqa: E402

FIX = Path(__file__).resolve().parent / "screenshot-fixtures"
# Matches SHARE_TOKEN in capture-screenshots.mjs (TOKEN_RE: [A-Za-z0-9_-]{16,64}).
TOKEN = "prs-powerball-demo-k3J9xQ"
CREATED_AT = "2026-07-17T00:12:31Z"


def main() -> None:
    state = json.loads((FIX / "creative-state.current.json").read_text())
    report = json.loads((FIX / "creative-eval-report.current.json").read_text())

    built = build_snapshot(
        state,
        report,
        token=TOKEN,
        concept_names=None,
        include_eval=True,
        now=CREATED_AT,
    )
    images = [f"{name.replace(' ', '_')}.jpg" for name in built.concept_names]
    for image in images:
        if not (FIX / "images" / image).is_file():
            raise SystemExit(f"missing fixture image: {image}")
    share = {"snapshot": built.snapshot, "images": images}
    (FIX / "share-fixture.json").write_text(json.dumps(share, indent=2) + "\n")

    concepts = state["final_visual_concepts"]["visual_concepts"]
    castable = {}
    for concept in concepts:
        name = concept["concept_name"]
        c = castability(state, name, DEFAULT_PERSON_SAFE_STYLES)
        entry = {"ok": c.ok, "reason": c.reason, "code": c.code, "style": c.style}
        if c.mode:
            entry["mode"] = c.mode
        if c.cast_consent_id:
            entry["cast_consent_id"] = c.cast_consent_id
        castable[name] = entry
    listing = {
        "variants": {},
        "castable": castable,
        "person_safe_styles": list(DEFAULT_PERSON_SAFE_STYLES),
    }
    (FIX / "variants-listing.json").write_text(json.dumps(listing, indent=2) + "\n")
    print(
        f"wrote share-fixture.json ({len(images)} creatives) and variants-listing.json"
    )
    for name, entry in castable.items():
        print(f"  {name}: {entry['ok']} {entry['code']} {entry['style']!r}")


if __name__ == "__main__":
    main()
