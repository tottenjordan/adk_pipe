import { describe, expect, it } from "vitest"

import { DIMENSION_LABELS, dimensionLabel } from "@/lib/eval-dimensions"

describe("dimensionLabel", () => {
  it("covers all 12 creative_eval dimensions", () => {
    expect(Object.keys(DIMENSION_LABELS)).toHaveLength(12)
  })

  it.each([
    ["strategic_alignment", "Strategy fit"],
    ["trend_authenticity", "Trend authenticity"],
    ["platform_viability", "Platform fit"],
    ["copy_quality", "Copy quality"],
    ["audience_fit", "Audience fit"],
    ["call_to_action_strength", "Call to action"],
    ["trend_visual_connection", "Trend connection"],
    ["brand_product_representation", "Brand & product"],
    ["audience_appeal", "Audience appeal"],
    ["prompt_technical_quality", "Prompt quality"],
    ["stopping_power", "Stopping power"],
    ["concept_coherence", "Coherence"],
  ])("maps %s to %s", (dim, label) => {
    expect(dimensionLabel(dim)).toBe(label)
  })

  it("falls back to sentence case for unknown snake_case names", () => {
    expect(dimensionLabel("emotional_resonance_score")).toBe("Emotional resonance score")
    expect(dimensionLabel("ALL_CAPS__name_")).toBe("All caps name")
    expect(dimensionLabel("")).toBe("")
  })

  it("ignores inherited object keys", () => {
    expect(dimensionLabel("constructor")).toBe("Constructor")
  })
})
