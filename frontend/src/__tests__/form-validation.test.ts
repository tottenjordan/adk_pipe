import { describe, it, expect } from "vitest";
import { isFormValid } from "@/lib/form-validation";
import type { CampaignInput } from "@/lib/types";

const base: CampaignInput = {
  agent: "trend_scout",
  brand: "PRS Guitars",
  targetAudience: "Musicians aged 25-45",
  targetProduct: "PRS SE CE24",
  keySellingPoints: "Great tone, versatile",
  targetSearchTrend: "",
};

const AGENTS = ["trend_scout", "creative_agent", "interactive_creative"] as const;
const CREATIVE = ["creative_agent", "interactive_creative"] as const;
const WITH_TREND = { targetSearchTrend: "tswift engaged" };

describe("isFormValid", () => {
  it("is valid for trend_scout without a trend", () => {
    expect(isFormValid(base)).toBe(true);
  });

  it.each(CREATIVE)("needs a trend for %s", (agent) => {
    expect(isFormValid({ ...base, agent })).toBe(false);
    expect(isFormValid({ ...base, agent, targetSearchTrend: "   " })).toBe(false);
    expect(isFormValid({ ...base, agent, ...WITH_TREND })).toBe(true);
  });

  it("does not need targetSearchTrend set at all for trend_scout", () => {
    const { targetSearchTrend: _unused, ...rest } = base;
    void _unused;
    expect(isFormValid(rest)).toBe(true);
  });

  for (const agent of AGENTS) {
    describe(agent, () => {
      const valid = { ...base, agent, ...WITH_TREND };
      it.each(["brand", "targetAudience", "targetProduct", "keySellingPoints"] as const)(
        "is invalid when %s is empty or whitespace",
        (field) => {
          expect(isFormValid(valid)).toBe(true);
          expect(isFormValid({ ...valid, [field]: "" })).toBe(false);
          expect(isFormValid({ ...valid, [field]: "  \n " })).toBe(false);
        },
      );
    });
  }

  describe("reference image URIs", () => {
    it.each(CREATIVE)("blocks an invalid reference URI for %s", (agent) => {
      const valid = { ...base, agent, ...WITH_TREND };
      expect(isFormValid({ ...valid, referenceImageUri: "gs://b/p.png" })).toBe(true);
      expect(isFormValid({ ...valid, referenceImageUri: "bucket/p.png" })).toBe(false);
      expect(
        isFormValid({ ...valid, extraReferenceImages: [{ uri: "nope", role: "style" }] }),
      ).toBe(false);
      expect(
        isFormValid({ ...valid, extraReferenceImages: [{ uri: "  ", role: "" }] }),
      ).toBe(true);
    });

    it("ignores hidden reference rows for trend_scout", () => {
      expect(isFormValid({ ...base, referenceImageUri: "bucket/p.png" })).toBe(true);
    });
  });
});
