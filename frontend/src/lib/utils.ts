import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

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

/** A label→state-key mapping for a read-only metadata display. */
export interface DisplayFieldDef {
  label: string
  key: string
  /** Fallback state key when `key` is absent (e.g. singular/plural variants). */
  altKey?: string
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
        state[def.key] ?? (def.altKey ? state[def.altKey] : undefined),
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
  { label: "Art Direction", key: "visual_intent" },
  { label: "Brand Colors", key: "brand_colors" },
  { label: "Preferred Style", key: "visual_style_preference" },
  { label: "Avoid", key: "visual_avoid" },
  { label: "Aspect Ratio", key: "visual_aspect_ratio" },
  { label: "Reference Image", key: "reference_image_uri" },
  { label: "Reference Role", key: "reference_image_role" },
]
