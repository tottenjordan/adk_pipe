"""Allowlisted fail reasons a rater can pick (never free text into prompts).

The single source of truth for the fail-reason chips on the results-page rating
control; mirrored in ``frontend/src/lib/rating-reasons.ts`` (drift-tested by
``tests/test_rating_reasons_drift.py``). The learning step only ever reads these
enum values, never the free-text rating note.
"""

from __future__ import annotations

FAIL_REASONS: tuple[str, ...] = (
    "product_not_visible",
    "text_problem",
    "unwanted_logo",
    "weak_cta",
    "off_brief",
    "trend_unclear",
    "cluttered",
    "off_brand_tone",
    "artifacts",
    "other",
)
FAIL_REASON_LABELS: dict[str, str] = {
    "product_not_visible": "Product hard to see",
    "text_problem": "In-image text problems",
    "unwanted_logo": "Unwanted logo / trademark",
    "weak_cta": "Weak call to action",
    "off_brief": "Off-brief / wrong message",
    "trend_unclear": "Trend unclear",
    "cluttered": "Cluttered / weak composition",
    "off_brand_tone": "Off-brand tone",
    "artifacts": "Visual artifacts / quality",
    "other": "Other",
}
