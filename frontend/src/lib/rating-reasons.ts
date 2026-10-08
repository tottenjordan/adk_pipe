/**
 * Allowlisted fail reasons a rater can pick on the results-page rating control.
 * Mirrors `runserver/rating_reasons.py` (the source of truth; drift-tested by
 * `tests/test_rating_reasons_drift.py`). Only these enum values are ever used to
 * learn from ratings, never the free-text note.
 */
import type { RatingKind } from "./ratings";

export const FAIL_REASONS = [
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
] as const;

export type FailReason = (typeof FAIL_REASONS)[number];

export const FAIL_REASON_LABELS: Readonly<Record<FailReason, string>> = {
  product_not_visible: "Product hard to see",
  text_problem: "In-image text problems",
  unwanted_logo: "Unwanted logo / trademark",
  weak_cta: "Weak call to action",
  off_brief: "Off-brief / wrong message",
  trend_unclear: "Trend unclear",
  cluttered: "Cluttered / weak composition",
  off_brand_tone: "Off-brand tone",
  artifacts: "Visual artifacts / quality",
  other: "Other",
};

// Reasons that only make sense for the other kind (the API accepts any enum value).
const VISUAL_ONLY: ReadonlySet<FailReason> = new Set([
  "product_not_visible",
  "text_problem",
  "unwanted_logo",
  "cluttered",
  "artifacts",
]);
const COPY_ONLY: ReadonlySet<FailReason> = new Set(["weak_cta"]);

export function isFailReason(value: unknown): value is FailReason {
  return typeof value === "string" && (FAIL_REASONS as readonly string[]).includes(value);
}

/** The chips offered for one kind of creative, in enum order. */
export function failReasonsFor(kind: RatingKind): readonly FailReason[] {
  const skip = kind === "visual" ? COPY_ONLY : VISUAL_ONLY;
  return FAIL_REASONS.filter((r) => !skip.has(r));
}
