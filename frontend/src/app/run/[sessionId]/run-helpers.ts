/** Assign a color to each pipeline widget by keyword. */
export function widgetAccent(label: string): { dot: string; badge: string; text: string } {
  if (label.toLowerCase().includes("visual") && label.toLowerCase().includes("final"))
    return { dot: "bg-violet-500", badge: "bg-violet-50 text-violet-600 border-violet-200", text: "text-violet-600" };
  if (label.toLowerCase().includes("critique"))
    return { dot: "bg-orange-500", badge: "bg-orange-50 text-orange-600 border-orange-200", text: "text-orange-600" };
  if (label.toLowerCase().includes("visual"))
    return { dot: "bg-cyan-500", badge: "bg-cyan-50 text-cyan-600 border-cyan-200", text: "text-cyan-600" };
  if (label.toLowerCase().includes("ad copy"))
    return { dot: "bg-indigo-500", badge: "bg-indigo-50 text-indigo-600 border-indigo-200", text: "text-indigo-600" };
  return { dot: "bg-emerald-500", badge: "bg-emerald-50 text-emerald-600 border-emerald-200", text: "text-emerald-600" };
}

/** Extract the list of items from pipeline state data (dict with one key holding an array). */
export function extractItems(data: unknown): Record<string, unknown>[] | null {
  if (!data || typeof data !== "object") return null;
  const obj = data as Record<string, unknown>;
  const keys = Object.keys(obj);
  for (const k of keys) {
    if (Array.isArray(obj[k])) return obj[k] as Record<string, unknown>[];
  }
  return null;
}

/**
 * Normalize the candidate trend terms from session state `raw_gtrends`.
 * The backend stores a `string[]` of ~25 terms; guard against absent/malformed
 * values and drop empties so the panel renders cleanly.
 */
export function parseRawGtrends(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((t): t is string => typeof t === "string")
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
}
