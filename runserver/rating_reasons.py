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

# Reasons that only make sense for one kind of creative. The chips the UI offers
# per kind (frontend failReasonsFor, drift-tested) and the reasons the API keeps:
# an inapplicable one is dropped, not refused.
_VISUAL_ONLY = frozenset(
    {"product_not_visible", "text_problem", "unwanted_logo", "cluttered", "artifacts"}
)
_COPY_ONLY = frozenset({"weak_cta"})
FAIL_REASONS_BY_KIND: dict[str, tuple[str, ...]] = {
    "visual": tuple(r for r in FAIL_REASONS if r not in _COPY_ONLY),
    "ad_copy": tuple(r for r in FAIL_REASONS if r not in _VISUAL_ONLY),
}
