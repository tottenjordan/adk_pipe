/** Small shared number and list helpers for the experiment libraries. */

/** True for a finite number (not NaN, ±Infinity or a non-number). */
export const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** Round to 4 decimal places. */
export const round4 = (v: number): number => Math.round(v * 1e4) / 1e4;

/** "a", "a and b", "a, b and c" ("" for none). */
export function joinList(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}
