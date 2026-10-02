import type { CampaignInput } from "@/lib/types";

/**
 * Build the `createSession` initialState for a campaign run — pure, so it is
 * unit-testable without React.
 *
 * - Every agent: `ui_app` records which agent the form started. It is UI-only:
 *   the backend's state seeding only setdefaults its own keys, so it passes
 *   through untouched and run history can identify the agent (all apps share
 *   one session store, so the session's `appName` can't).
 * - trend_scout: seeds `interactive_trend_pick` when the user opts in.
 * - creative_agent / interactive_creative: maps each set visual-intent field to
 *   its snake_case state key (matching creative_agent/callbacks.py). Empty
 *   values are omitted (the backend setdefaults them to "").
 */
export function buildInitialState(form: CampaignInput): Record<string, unknown> {
  const state: Record<string, unknown> = { ui_app: form.agent };

  if (form.agent === "trend_scout") {
    if (form.interactiveTrendPick) state.interactive_trend_pick = true;
    return state;
  }

  if (form.agent !== "creative_agent" && form.agent !== "interactive_creative") {
    return state;
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

  for (const [field, key] of mapping) {
    const value = (form[field] as string | undefined)?.trim();
    if (value) {
      state[key] = value;
    }
  }

  return state;
}
