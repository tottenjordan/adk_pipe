import type { CampaignInput } from "@/lib/types";

/**
 * Build the `createSession` initialState for a campaign run — pure, so it is
 * unit-testable without React.
 *
 * The backend seeds these keys via `_set_initial_states` (setdefault), so only
 * non-empty values are worth sending. Returns `undefined` when there is nothing
 * to seed (keeps the existing "no initialState" call shape).
 *
 * - trend_scout: seeds `interactive_trend_pick` when the user opts in.
 * - creative_agent / interactive_creative: maps each set visual-intent field to
 *   its snake_case state key (matching creative_agent/callbacks.py).
 */
export function buildInitialState(
  form: CampaignInput,
): Record<string, unknown> | undefined {
  if (form.agent === "trend_scout") {
    return form.interactiveTrendPick
      ? { interactive_trend_pick: true }
      : undefined;
  }

  if (form.agent !== "creative_agent" && form.agent !== "interactive_creative") {
    return undefined;
  }

  // camelCase form field → snake_case session-state key.
  const mapping: Array<[keyof CampaignInput, string]> = [
    ["visualIntent", "visual_intent"],
    ["brandColors", "brand_colors"],
    ["visualStylePreference", "visual_style_preference"],
    ["visualAvoid", "visual_avoid"],
    ["visualAspectRatio", "visual_aspect_ratio"],
    ["referenceImageUri", "reference_image_uri"],
    ["referenceImageRole", "reference_image_role"],
  ];

  const state: Record<string, unknown> = {};
  for (const [field, key] of mapping) {
    const value = (form[field] as string | undefined)?.trim();
    if (value) {
      state[key] = value;
    }
  }

  return Object.keys(state).length > 0 ? state : undefined;
}
