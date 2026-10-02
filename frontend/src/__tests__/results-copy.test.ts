import { describe, expect, it } from "vitest"

import { campaignSummary, summarySentence } from "@/lib/results-copy"

const base = {
  total_ad_copies: 4,
  ad_copies_passed: 3,
  avg_ad_copy_score: 0.8,
  total_visual_concepts: 4,
  visual_concepts_passed: 2,
  avg_visual_score: 0.7,
  overall_pass_rate: 0.625,
  weakest_dimensions: [],
}

describe("summarySentence", () => {
  it("reads as one plain sentence", () => {
    expect(summarySentence(base)).toBe("3 of 4 ad copies and 2 of 4 visuals pass.")
  })

  it("singularises and drops visuals when there are none", () => {
    expect(
      summarySentence({ ...base, total_ad_copies: 1, ad_copies_passed: 1, total_visual_concepts: 0 })
    ).toBe("1 of 1 ad copy pass.")
    expect(
      summarySentence({ ...base, total_visual_concepts: 1, visual_concepts_passed: 0 })
    ).toBe("3 of 4 ad copies and 0 of 1 visual pass.")
  })
})

describe("campaignSummary", () => {
  const f = (key: string, value: string) => ({ key, value })

  it("joins brand, product and trend", () => {
    expect(
      campaignSummary([f("brand", "PRS"), f("target_product", "SE CE24"), f("target_search_trends", "Powerball")])
    ).toBe("PRS, SE CE24, on the Powerball trend")
  })

  it("drops missing parts", () => {
    expect(campaignSummary([f("brand", "PRS")])).toBe("PRS")
    expect(campaignSummary([f("target_search_trends", "Powerball")])).toBe("Powerball trend")
    expect(campaignSummary([])).toBe("")
  })
})
