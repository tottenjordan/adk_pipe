"""A real finished creative run (the screenshot-harness fixtures: PRS SE CE24 x
Powerball) as session state + eval report, shared by the shares tests."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "frontend/scripts/screenshot-fixtures"
BUCKET = "trend-trawler-deploy-ae"  # the fixture's eval_report_gcs_uri bucket
CONCEPTS = (
    "The Golden Golf Cart Gig",
    "Nihilistic Retirement Plan",
    "The Jackpot Reveal",
    "The Authentic Encore",
)


def creative_report() -> dict:
    return json.loads((FIXTURES / "creative-eval-report.json").read_text())


def image_uri(i: int, bucket: str = BUCKET) -> str:
    return f"gs://{bucket}/creative_agent/run/images/{i}.png"


def creative_state(*, with_report: bool = True, with_images: bool = True) -> dict:
    """The fixture state, plus ``generated_images`` for every concept (the
    fixture predates that key) unless ``with_images`` is False."""
    state = json.loads((FIXTURES / "creative-state.json").read_text())
    if with_report:
        state["creative_evaluation_report"] = creative_report()
    if with_images:
        state["generated_images"] = {
            name: {"gcs_uri": image_uri(i), "artifact_key": f"{i}.png", "attempts": 1}
            for i, name in enumerate(CONCEPTS)
        }
    return state
