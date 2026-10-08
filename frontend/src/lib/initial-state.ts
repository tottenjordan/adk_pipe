import { isReferenceUri, referenceImagesFromForm } from "@/lib/reference-images";
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
 * - creative_agent / interactive_creative: maps the core campaign fields
 *   (brand, audience, product, selling points, trend) and each set
 *   visual-intent field to its snake_case state key (matching
 *   creative_agent/callbacks.py). Empty values are omitted (the backend
 *   setdefaults them to ""). Seeding the campaign fields makes the inputs
 *   deterministic; the kickoff message only echoes them. trend_scout's state
 *   init overwrites these keys, so they are not seeded for it.
 * - Rating learning: `learn_from_ratings: true` only when a creative run opts
 *   in (the backend setdefaults it to false).
 * - Reference images: every set reference row goes to `reference_images`
 *   (`[{uri, role}]`, max 3); row 1 is also sent as the legacy
 *   `reference_image_uri`/`reference_image_role` pair for one release (the
 *   backend dedupes the two).
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
    ["brand", "brand"],
    ["targetAudience", "target_audience"],
    ["targetProduct", "target_product"],
    ["keySellingPoints", "key_selling_points"],
    ["targetSearchTrend", "target_search_trends"],
    ["visualIntent", "visual_intent"],
    ["brandColors", "brand_colors"],
    ["visualStylePreference", "visual_style_preference"],
    ["visualAvoid", "visual_avoid"],
    ["visualAspectRatio", "visual_aspect_ratio"],
  ];

  for (const [field, key] of mapping) {
    const value = (form[field] as string | undefined)?.trim();
    if (value) {
      state[key] = value;
    }
  }

  // Row 1 as the legacy pair, only when its URI is valid (never emit an
  // invalid reference; the form blocks submit on one anyway).
  const legacyUri = form.referenceImageUri?.trim() ?? "";
  if (isReferenceUri(legacyUri)) {
    state.reference_image_uri = legacyUri;
    const legacyRole = form.referenceImageRole?.trim();
    if (legacyRole) state.reference_image_role = legacyRole;
  }

  if (form.learnFromRatings === true) state.learn_from_ratings = true;

  const references = referenceImagesFromForm(form);
  if (references.length) state.reference_images = references;

  return state;
}
