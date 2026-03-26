import { describe, it, expect } from "vitest";

// Replicate form validation logic from page.tsx
interface CampaignInput {
  agent: "trend_trawler" | "creative_agent";
  brand: string;
  targetAudience: string;
  targetProduct: string;
  keySellingPoints: string;
  targetSearchTrend: string;
}

function isFormValid(form: CampaignInput): boolean {
  return !!(
    form.brand &&
    form.targetAudience &&
    form.targetProduct &&
    form.keySellingPoints &&
    (form.agent !== "creative_agent" || form.targetSearchTrend)
  );
}

describe("form validation", () => {
  const base: CampaignInput = {
    agent: "trend_trawler",
    brand: "PRS Guitars",
    targetAudience: "Musicians aged 25-45",
    targetProduct: "PRS SE CE24",
    keySellingPoints: "Great tone, versatile",
    targetSearchTrend: "",
  };

  it("is valid for trend_trawler with all required fields", () => {
    expect(isFormValid(base)).toBe(true);
  });

  it("is valid for trend_trawler even without targetSearchTrend", () => {
    expect(isFormValid({ ...base, targetSearchTrend: "" })).toBe(true);
  });

  it("is invalid when brand is empty", () => {
    expect(isFormValid({ ...base, brand: "" })).toBe(false);
  });

  it("is invalid when targetAudience is empty", () => {
    expect(isFormValid({ ...base, targetAudience: "" })).toBe(false);
  });

  it("is invalid when targetProduct is empty", () => {
    expect(isFormValid({ ...base, targetProduct: "" })).toBe(false);
  });

  it("is invalid when keySellingPoints is empty", () => {
    expect(isFormValid({ ...base, keySellingPoints: "" })).toBe(false);
  });

  it("is invalid for creative_agent without targetSearchTrend", () => {
    expect(
      isFormValid({ ...base, agent: "creative_agent", targetSearchTrend: "" })
    ).toBe(false);
  });

  it("is valid for creative_agent with targetSearchTrend", () => {
    expect(
      isFormValid({
        ...base,
        agent: "creative_agent",
        targetSearchTrend: "tswift engaged",
      })
    ).toBe(true);
  });
});
