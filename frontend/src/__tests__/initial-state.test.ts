import { describe, it, expect } from "vitest";
import { buildInitialState } from "@/lib/initial-state";
import type { CampaignInput } from "@/lib/types";

const base: CampaignInput = {
  agent: "creative_agent",
  brand: "PRS Guitars",
  targetAudience: "Musicians",
  targetProduct: "PRS SE CE24",
  keySellingPoints: "Great tone",
  targetSearchTrend: "tswift engaged",
};

// The core campaign fields seeded for the creative agents (snake_case keys
// matching creative_agent/callbacks.py, which setdefaults rather than blanks them).
const campaign = {
  brand: "PRS Guitars",
  target_audience: "Musicians",
  target_product: "PRS SE CE24",
  key_selling_points: "Great tone",
  target_search_trends: "tswift engaged",
};

describe("buildInitialState", () => {
  it("seeds the agent and core campaign fields for a plain creative run", () => {
    expect(buildInitialState(base)).toEqual({ ui_app: "creative_agent", ...campaign });
  });

  it("trims campaign fields and omits empty ones", () => {
    const state = buildInitialState({
      ...base,
      brand: "  PRS Guitars  ",
      keySellingPoints: "   ",
      targetSearchTrend: undefined,
    });
    expect(state).toEqual({
      ui_app: "creative_agent",
      brand: "PRS Guitars",
      target_audience: "Musicians",
      target_product: "PRS SE CE24",
    });
  });

  it("records ui_app for every agent", () => {
    for (const agent of ["trend_scout", "creative_agent", "interactive_creative"] as const) {
      expect(buildInitialState({ ...base, agent }).ui_app).toBe(agent);
    }
  });

  it("maps set visual-intent fields to snake_case keys", () => {
    const state = buildInitialState({
      ...base,
      visualIntent: "moody film noir",
      brandColors: "#1a1a1a and gold",
      visualStylePreference: "cinematic",
      visualAvoid: "clutter",
      visualAspectRatio: "1:1",
      referenceImageUri: "gs://b/logo.png",
      referenceImageRole: "logo",
    });
    expect(state).toEqual({
      ui_app: "creative_agent",
      ...campaign,
      visual_intent: "moody film noir",
      brand_colors: "#1a1a1a and gold",
      visual_style_preference: "cinematic",
      visual_avoid: "clutter",
      visual_aspect_ratio: "1:1",
      reference_image_uri: "gs://b/logo.png",
      reference_image_role: "logo",
      reference_images: [{ uri: "gs://b/logo.png", role: "logo" }],
    });
  });

  it("emits every reference row as reference_images (legacy keys from row 1)", () => {
    const state = buildInitialState({
      ...base,
      referenceImageUri: "gs://b/p.png",
      referenceImageRole: "product",
      extraReferenceImages: [
        { uri: "https://x/style.jpg", role: "style" },
        { uri: "  ", role: "logo" },
      ],
    });
    expect(state.reference_image_uri).toBe("gs://b/p.png");
    expect(state.reference_image_role).toBe("product");
    expect(state.reference_images).toEqual([
      { uri: "gs://b/p.png", role: "product" },
      { uri: "https://x/style.jpg", role: "style" },
    ]);
  });

  it("does not seed reference images for trend_scout", () => {
    const state = buildInitialState({
      ...base,
      agent: "trend_scout",
      extraReferenceImages: [{ uri: "gs://b/s.png", role: "style" }],
    });
    expect(state.reference_images).toBeUndefined();
  });

  it("omits empty / whitespace-only fields and trims values", () => {
    const state = buildInitialState({
      ...base,
      visualIntent: "  bold retro  ",
      brandColors: "   ",
      visualAspectRatio: "",
    });
    expect(state).toEqual({ ui_app: "creative_agent", ...campaign, visual_intent: "bold retro" });
  });

  it("works for interactive_creative too", () => {
    const state = buildInitialState({
      ...base,
      agent: "interactive_creative",
      visualAspectRatio: "16:9",
    });
    expect(state).toEqual({
      ui_app: "interactive_creative",
      ...campaign,
      visual_aspect_ratio: "16:9",
    });
  });

  // trend_scout's own state init overwrites these keys, so they are not seeded.
  it("ignores campaign and visual-intent fields for trend_scout", () => {
    const state = buildInitialState({
      ...base,
      agent: "trend_scout",
      visualIntent: "ignored",
    });
    expect(state).toEqual({ ui_app: "trend_scout" });
  });

  it("keeps trend_scout interactive-trend-pick seeding", () => {
    const state = buildInitialState({
      ...base,
      agent: "trend_scout",
      interactiveTrendPick: true,
    });
    expect(state).toEqual({ ui_app: "trend_scout", interactive_trend_pick: true });
  });
});
