/**
 * Staleness of Google's public trends table (trend_scout state
 * `gtrends_staleness` = `{refresh_date, age_days, stale, threshold_days}`,
 * written by `get_daily_gtrends`; agents/trend_scout/staleness.py). Google
 * sometimes stops updating the table for days; the run page and the trend
 * pick panel then say the trends may be out of date.
 */

import { parseJsonObject } from "@/lib/trend-flags";
import { formatRefreshDate, parseRefreshDate } from "@/lib/trend-summaries";

export type GtrendsStaleness = { refreshDate: string; ageDays: number; stale: boolean };

/** The state value (object or JSON string), or null when missing/malformed. Never throws. */
export function parseGtrendsStaleness(raw: unknown): GtrendsStaleness | null {
  const obj = parseJsonObject(raw);
  if (!obj) return null;
  const refreshDate = parseRefreshDate(obj.refresh_date);
  const ageDays = obj.age_days;
  const stale = obj.stale;
  if (!refreshDate || typeof stale !== "boolean") return null;
  if (typeof ageDays !== "number" || !Number.isInteger(ageDays) || ageDays < 0) return null;
  return { refreshDate, ageDays, stale };
}

/**
 * "Google's trend data was last updated Oct 13 (6 days ago); trends may be out
 * of date." for a stale table; "" when fresh, missing or malformed.
 */
export function trendStalenessText(state: Record<string, unknown>): string {
  const value = parseGtrendsStaleness(state.gtrends_staleness);
  if (!value?.stale) return "";
  const unit = value.ageDays === 1 ? "day" : "days";
  return (
    `Google's trend data was last updated ${formatRefreshDate(value.refreshDate)} ` +
    `(${value.ageDays} ${unit} ago); trends may be out of date.`
  );
}
