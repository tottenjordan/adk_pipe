/**
 * The opt-in "respectful angle" for a sensitive trend (session state written
 * by the creative agents; every key is absent on older runs, so read
 * defensively):
 * - `safer_angle_active`: "yes" when the run is looking for a respectful angle.
 * - `creative_brief.trend_bridge.safer_angle`: the angle the brief chose, ""
 *   when none was found (the creatives then never reference the event).
 * - `safer_angle_named_copies`: ad copy ids whose text names the real person
 *   (a respectful tribute only).
 */

import { parseJsonObject } from "@/lib/trend-flags";

/** Shown when the run looked for a respectful angle but found none. */
export const SAFER_ANGLE_NONE = "none found — the event is never referenced";

export function saferAngleActive(state: Record<string, unknown>): boolean {
  return state.safer_angle_active === "yes";
}

/**
 * The text after "Safer angle:" — the brief's angle, {@link SAFER_ANGLE_NONE}
 * once a brief exists without one, or "" (render nothing) when the run is not
 * looking for one or has no brief yet.
 */
export function saferAngleText(state: Record<string, unknown>): string {
  if (!saferAngleActive(state)) return "";
  const brief = parseJsonObject(state.creative_brief);
  if (!brief) return "";
  const bridge = parseJsonObject(brief.trend_bridge);
  const angle = typeof bridge?.safer_angle === "string" ? bridge.safer_angle.trim() : "";
  return angle || SAFER_ANGLE_NONE;
}

/** `safer_angle_named_copies` as a set of id strings (numbers and strings alike). */
export function namedCopyIds(raw: unknown): Set<string> {
  const ids = new Set<string>();
  if (!Array.isArray(raw)) return ids;
  for (const id of raw) {
    if (typeof id === "number" && Number.isFinite(id)) ids.add(String(id));
    else if (typeof id === "string" && id.trim()) ids.add(id.trim());
  }
  return ids;
}
