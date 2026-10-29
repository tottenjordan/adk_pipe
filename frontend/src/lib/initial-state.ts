import { isReferenceUri, referenceImagesFromForm } from "@/lib/reference-images";
import { contextSensitivity } from "@/lib/trend-flags";
import type { CampaignInput } from "@/lib/types";

/**
 * Default campaign form state on the home page. Explicit boolean opt-ins default
 * to `false` so submitting the form with the checkboxes unchecked sends
 * `enterprise_grounding_enabled: false` (preventing server-configured Discovery
 * Engine env vars from turning enterprise grounding ON behind the user's back).
 */
export const EMPTY_CAMPAIGN_FORM: CampaignInput = {
  agent: "trend_scout",
  brand: "",
  targetAudience: "",
  targetProduct: "",
  keySellingPoints: "",
  targetSearchTrend: "",
  interactiveTrendPick: false,
  trendCountryCode: "US",
  trendRegion: "",
  enterpriseGroundingEnabled: false,
  learnFromRatings: false,
  saferAngle: false,
  personReference: null,
  referenceImageUri: "",
  referenceImageRole: "",
  extraReferenceImages: [],
  visualIntent: "",
  brandColors: "",
  visualStylePreference: "",
  visualAvoid: "",
  visualAspectRatio: "",
};

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
 * - Trend card hand-off: trend_scout's context + detection date for the trend
 *   (`trend_scout_context` / `trend_detected_on`, hints the agents re-verify).
 * - Rating learning: `learn_from_ratings: true` only when a creative run opts
 *   in (the backend setdefaults it to false).
 * - Safer angle: `safer_angle_opt_in: true` only when a creative run opts in
 *   AND the carried trend context is flagged sensitive (otherwise the key is
 *   omitted; the backend treats it as off).
 * - Person: `person_reference: {uri, consent_id}` only when a creative run
 *   picks one of the caller's consents (the backend setdefaults it to {}).
 * - Reference images: every set reference row goes to `reference_images`
 *   (`[{uri, role}]`, max 3); row 1 is also sent as the legacy
 *   `reference_image_uri`/`reference_image_role` pair for one release (the
 *   backend dedupes the two).
 */
export function buildInitialState(form: CampaignInput): Record<string, unknown> {
  const state: Record<string, unknown> = { ui_app: form.agent };

  if (form.agent === "trend_scout") {
    if (form.interactiveTrendPick) state.interactive_trend_pick = true;
    const country = form.trendCountryCode?.trim().toUpperCase();
    if (country && /^[A-Z]{2}$/.test(country) && country !== "US") {
      state.trend_country_code = country;
    }
    const region = form.trendRegion?.trim().replace(/\s+/g, " ");
    if (region) {
      state.trend_region = region.slice(0, 80);
    }
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
    ["trendContext", "trend_scout_context"],
    ["trendDetectedOn", "trend_detected_on"],
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

  if (typeof form.enterpriseGroundingEnabled === "boolean") {
    state.enterprise_grounding_enabled = form.enterpriseGroundingEnabled;
  }
  if (form.learnFromRatings === true) state.learn_from_ratings = true;
  if (form.saferAngle === true && contextSensitivity(form.trendContext).sensitive) {
    state.safer_angle_opt_in = true;
  }

  const person = form.personReference;
  if (person?.uri?.trim() && person.consentId?.trim()) {
    state.person_reference = { uri: person.uri.trim(), consent_id: person.consentId.trim() };
  }

  const references = referenceImagesFromForm(form);
  if (references.length) state.reference_images = references;

  return state;
}
