/**
 * trend_scout's sensitive-trend flags (state `trend_flags`) and the matching
 * `sensitive` / `sensitive_reason` keys of a trend's `trend_context` JSON.
 *
 * `trend_flags` = `{"flags": [{"term", "sensitive", "reason"}]}`, possibly a
 * JSON string (ADK output_key) or a fenced ```json string.
 */

const FENCE = /^\s*```(?:json)?\s*|\s*```\s*$/gi;

/**
 * Case/space/quote-insensitive key for matching a term (mirrors
 * trend_scout/trend_context.py `_norm_term`).
 */
export function normalizeTerm(term: unknown): string {
  if (typeof term !== "string") return "";
  return term
    .trim()
    .replace(/^["'`*[\]]+|["'`*[\]]+$/g, "")
    .split(/\s+/)
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

/** A plain object from an object, JSON string or fenced JSON string; null otherwise. */
export function parseJsonObject(raw: unknown): Record<string, unknown> | null {
  let data = raw;
  if (typeof data === "string") {
    try {
      data = JSON.parse(data.replace(FENCE, ""));
    } catch {
      return null;
    }
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  return data as Record<string, unknown>;
}

/** Sensitive flags only: normalised term → reason ("" when none given). */
export function parseTrendFlags(raw: unknown): Map<string, string> {
  const result = new Map<string, string>();
  const flags = parseJsonObject(raw)?.flags;
  if (!Array.isArray(flags)) return result;
  for (const flag of flags) {
    if (!flag || typeof flag !== "object") continue;
    const { term, sensitive, reason } = flag as Record<string, unknown>;
    const key = normalizeTerm(term);
    if (!key || sensitive !== true || result.has(key)) continue;
    result.set(key, typeof reason === "string" ? reason.trim() : "");
  }
  return result;
}

export interface Sensitivity {
  sensitive: boolean;
  reason: string;
}

/** Whether a trend's `trend_context` JSON marks it sensitive, and why. */
export function contextSensitivity(trendContextJson: string | undefined): Sensitivity {
  const data = parseJsonObject(trendContextJson ?? "");
  if (data?.sensitive !== true) return { sensitive: false, reason: "" };
  const reason = typeof data.sensitive_reason === "string" ? data.sensitive_reason.trim() : "";
  return { sensitive: true, reason };
}
