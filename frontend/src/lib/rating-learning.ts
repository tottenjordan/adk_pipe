/**
 * What opt-in rating-driven learning did to a run, read from session state:
 * `learn_from_ratings` (the per-run opt-in) and `rating_signals_applied` (the
 * learning step's record — `{ratings, applied: true, signals, strictness,
 * styles_excluded, styles_preferred}` or `{ratings?, applied: false, reason}`).
 * Pure; malformed values degrade to "pending" / empty lists, never throw.
 */
import { STRICTNESS_LABELS, isFailReason, type FailReason } from "./rating-reasons";

export type RatingLearning =
  | { status: "off" }
  | { status: "pending" }
  | { status: "unavailable" }
  | { status: "not_enough"; ratings: number }
  | { status: "no_effects"; ratings: number }
  | {
      status: "applied";
      ratings: number;
      guidance: boolean;
      preferred: string[];
      avoided: string[];
      strictness: FailReason[];
    };

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string" && v.trim() !== "")
    : [];
}

export function describeRatingLearning(state: Record<string, unknown>): RatingLearning {
  if (state.learn_from_ratings !== true) return { status: "off" };
  const rec = state.rating_signals_applied;
  if (!rec || typeof rec !== "object" || Array.isArray(rec)) return { status: "pending" };
  const r = rec as Record<string, unknown>;
  const ratings = typeof r.ratings === "number" ? r.ratings : 0;
  if (r.applied === true) {
    return {
      status: "applied",
      ratings,
      guidance: typeof r.signals === "string" && r.signals.trim() !== "",
      preferred: strings(r.styles_preferred),
      avoided: strings(r.styles_excluded),
      strictness: strings(r.strictness).filter(
        (f): f is FailReason => isFailReason(f) && f in STRICTNESS_LABELS,
      ),
    };
  }
  if (r.reason === "not_enough_ratings") return { status: "not_enough", ratings };
  if (r.reason === "unavailable") return { status: "unavailable" };
  if (r.reason === "no_effects") return { status: "no_effects", ratings };
  return { status: "pending" };
}

/**
 * The one-line summary shown with a run's outputs, or null when there is
 * nothing to say (off, still pending, or no effect enabled).
 */
export function learningSummaryText(state: Record<string, unknown>): string | null {
  const l = describeRatingLearning(state);
  switch (l.status) {
    case "applied": {
      const head = `Learned from ${l.ratings} team rating${l.ratings === 1 ? "" : "s"}`;
      const parts: string[] = [];
      if (l.preferred.length) parts.push(`preferred ${l.preferred.join(", ")}`);
      if (l.avoided.length) parts.push(`avoided ${l.avoided.join(", ")}`);
      if (l.strictness.length) {
        parts.push(`stricter checks: ${l.strictness.map((f) => STRICTNESS_LABELS[f]).join(", ")}`);
      }
      if (!parts.length && l.guidance) parts.push("guidance for the brief, copy and art direction");
      return parts.length ? `${head}: ${parts.join("; ")}.` : `${head}.`;
    }
    case "not_enough":
      return `Not enough ratings yet (${l.ratings}).`;
    case "unavailable":
      return "Ratings couldn't be read, so nothing was learned.";
    default:
      return null;
  }
}
