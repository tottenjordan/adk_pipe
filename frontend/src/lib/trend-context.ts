// The compact per-trend `trend_context` JSON (seeded as `trend_scout_context`).
// Mirrors trend_scout/trend_context.py (the BigQuery `trend_context` value).

/** Per-field character cap, as in the backend. */
export const MAX_TREND_FIELD_CHARS = 400;

/** The string fields, in the canonical order. */
const STRING_FIELDS = ["context", "cultural_angle", "event_hint", "hook"] as const;

/** A single-line, capped string ("" for non-strings). */
export function cleanTrendField(value: unknown): string {
  if (typeof value !== "string") return "";
  return value
    .split(/\s+/)
    .filter(Boolean)
    .join(" ")
    .slice(0, MAX_TREND_FIELD_CHARS)
    .trimEnd();
}

/**
 * A `trendContext` URL param reduced to the allowlisted shape before it is seeded
 * into the prompts: `context`, `cultural_angle`, `event_hint`, `hook` (cleaned and
 * capped like the trend cards, blanks dropped), `sensitive` only as a boolean and
 * `sensitive_reason` only as a string. Anything else is stripped; invalid JSON, a
 * non-object or nothing left → "" (no trend context at all).
 */
export function sanitizeTrendContext(raw: string | null | undefined): string {
  if (!raw) return "";
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return "";
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return "";
  const source = data as Record<string, unknown>;
  const out: Record<string, string | boolean> = {};
  for (const key of STRING_FIELDS) {
    const value = cleanTrendField(source[key]);
    if (value) out[key] = value;
  }
  if (typeof source.sensitive === "boolean") out.sensitive = source.sensitive;
  const reason = cleanTrendField(source.sensitive_reason);
  if (reason) out.sensitive_reason = reason;
  return Object.keys(out).length ? JSON.stringify(out) : "";
}
