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

describe("buildInitialState", () => {
  it("returns undefined for a plain creative run with no intent", () => {
    expect(buildInitialState(base)).toBeUndefined();
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
      visual_intent: "moody film noir",
      brand_colors: "#1a1a1a and gold",
      visual_style_preference: "cinematic",
      visual_avoid: "clutter",
      visual_aspect_ratio: "1:1",
      reference_image_uri: "gs://b/logo.png",
      reference_image_role: "logo",
    });
  });

  it("omits empty / whitespace-only fields and trims values", () => {
    const state = buildInitialState({
      ...base,
      visualIntent: "  bold retro  ",
      brandColors: "   ",
      visualAspectRatio: "",
    });
    expect(state).toEqual({ visual_intent: "bold retro" });
  });

  it("works for interactive_creative too", () => {
    const state = buildInitialState({
      ...base,
      agent: "interactive_creative",
      visualAspectRatio: "16:9",
    });
    expect(state).toEqual({ visual_aspect_ratio: "16:9" });
  });

  it("ignores visual-intent fields for trend_scout", () => {
    const state = buildInitialState({
      ...base,
      agent: "trend_scout",
      visualIntent: "ignored",
    });
    expect(state).toBeUndefined();
  });

  it("keeps trend_scout interactive-trend-pick seeding", () => {
    const state = buildInitialState({
      ...base,
      agent: "trend_scout",
      interactiveTrendPick: true,
    });
    expect(state).toEqual({ interactive_trend_pick: true });
  });
});
