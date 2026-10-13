/**
 * trend_scout's per-term "why it's trending" summaries (state
 * `trend_summaries`) and the Google Trends refresh date (state
 * `gtrends_refresh_date`).
 *
 * `trend_summaries` = `{"refresh_date", "source": "cache"|"fresh"|"unavailable",
 * "items": [{"term", "summary"}]}`, possibly a JSON string (ADK state) or a
 * fenced ```json string. Missing or malformed → no summaries.
 */

import { normalizeTerm, parseJsonObject } from "@/lib/trend-flags";

/** Longest summary shown; longer ones are cut with an ellipsis. */
export const MAX_SUMMARY_CHARS = 300;

function capSummary(text: string): string {
  const clean = text.split(/\s+/).filter(Boolean).join(" ");
  if (clean.length <= MAX_SUMMARY_CHARS) return clean;
  return `${clean.slice(0, MAX_SUMMARY_CHARS - 1).trimEnd()}…`;
}

/** Normalised term → summary (first non-blank one per term). Never throws. */
export function parseTrendSummaries(raw: unknown): Map<string, string> {
  const result = new Map<string, string>();
  const items = parseJsonObject(raw)?.items;
  if (!Array.isArray(items)) return result;
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const { term, summary } = item as Record<string, unknown>;
    const key = normalizeTerm(term);
    if (!key || typeof summary !== "string" || result.has(key)) continue;
    const text = capSummary(summary);
    if (text) result.set(key, text);
  }
  return result;
}

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** A valid "YYYY-MM-DD" date string (trimmed), else null. */
export function parseRefreshDate(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const value = raw.trim();
  const match = ISO_DATE.exec(value);
  if (!match) return null;
  const [year, month, day] = match.slice(1).map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  ) {
    return null;
  }
  return value;
}

/** "2026-10-13" → "Oct 13" (calendar date, no timezone shift); "" when invalid. */
export function formatRefreshDate(raw: unknown): string {
  const value = parseRefreshDate(raw);
  if (!value) return "";
  const [year, month, day] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}
