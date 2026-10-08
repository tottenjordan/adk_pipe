import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"
import { describeRatingLearning } from "@/lib/rating-learning"
import { formatReferenceImages } from "@/lib/reference-images"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Normalize an ADK session-state value into a display string.
 * Most campaign fields are plain strings, but some (e.g. `target_search_trends`)
 * are stored nested as `{ target_search_trends: string[] }`. Rendering those raw
 * crashes React with "Objects are not valid as a React child", so flatten arrays
 * and unwrap such wrapper objects into a display string.
 */
export function formatStateValue(value: unknown): string {
  if (value == null) return ""
  if (typeof value === "string") return value
  if (Array.isArray(value)) return value.map(formatStateValue).filter(Boolean).join(", ")
  if (typeof value === "object") {
    return Object.values(value as Record<string, unknown>)
      .map(formatStateValue)
      .filter(Boolean)
      .join(", ")
  }
  return String(value)
}

/**
 * True when the image step exhausted all its retries and produced no visuals
 * (issue #116). `visual_generator_resilient` (a RetryUntilKeyAgent) writes the
 * `_images_generated__retry_exhausted` marker to session state on exhaustion, so
 * a "successful" run can still ship an empty gallery — this lets the results page
 * flag that clearly. Coerced to a plain boolean since ADK may serialize the
 * marker as a truthy non-bool.
 */
export function imagesRetryExhausted(state: Record<string, unknown>): boolean {
  return Boolean(state["_images_generated__retry_exhausted"])
}

/** A label→state-key mapping for a read-only metadata display. */
export interface DisplayFieldDef {
  label: string
  key: string
  /** Fallback state key when `key` is absent (e.g. singular/plural variants). */
  altKey?: string
  /** Derive the value from the whole state instead of `key`/`altKey`. */
  value?: (state: Record<string, unknown>) => unknown
}

/** A resolved display field ready to render. */
export interface DisplayField {
  label: string
  key: string
  value: string
}

/**
 * Resolve a set of {@link DisplayFieldDef}s against a session-state object into
 * renderable {@link DisplayField}s, dropping any whose value is empty. Values are
 * normalized with {@link formatStateValue}, so unset keys (which default to `""`)
 * collapse out — an unseeded run simply shows nothing.
 */
export function buildDisplayFields(
  state: Record<string, unknown>,
  defs: DisplayFieldDef[],
): DisplayField[] {
  return defs
    .map((def) => ({
      label: def.label,
      key: def.key,
      value: formatStateValue(
        def.value
          ? def.value(state)
          : (state[def.key] ?? (def.altKey ? state[def.altKey] : undefined)),
      ),
    }))
    .filter((f) => f.value !== "")
}

/**
 * Optional user visual art-direction inputs (PR #114), surfaced read-only
 * alongside campaign metadata. Unset keys default to `""` in session state, so
 * `buildDisplayFields` filters them out and non-creative runs show nothing.
 */
export const VISUAL_DIRECTION_FIELDS: DisplayFieldDef[] = [
  { label: "Art direction", key: "visual_intent" },
  { label: "Brand colors", key: "brand_colors" },
  { label: "Preferred style", key: "visual_style_preference" },
  { label: "Avoid", key: "visual_avoid" },
  { label: "Aspect ratio", key: "visual_aspect_ratio" },
  // Legacy single reference + `reference_images`, merged and deduped.
  { label: "Reference images", key: "reference_images", value: formatReferenceImages },
]

/**
 * The run's rating-learning status for the metadata display: `""` (hidden)
 * unless the run opted in (`learn_from_ratings`), else "On" plus what the
 * learning step recorded in `rating_signals_applied` (shared parsing with the
 * outputs' `LearningSummary`, which shows what was learned in full).
 */
export function formatRatingLearning(state: Record<string, unknown>): string {
  const l = describeRatingLearning(state)
  switch (l.status) {
    case "off":
      return ""
    case "applied":
      return `On: learned from ${l.ratings} ratings`
    case "not_enough":
      return `On: not enough ratings yet (${l.ratings}${l.min === null ? "" : ` of ${l.min}`})`
    case "unavailable":
      return "On: ratings unavailable"
    default:
      return "On"
  }
}

/** Campaign-metadata row for the per-run "learn from past ratings" opt-in. */
export const RATING_LEARNING_FIELD: DisplayFieldDef = {
  label: "Learn from ratings",
  key: "learn_from_ratings",
  value: formatRatingLearning,
}
