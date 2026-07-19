import { describe, expect, it } from "vitest"

import { nonImageWarnings } from "@/lib/utils"

/**
 * `nonImageWarnings` filters the eval report's degradation warnings down to the
 * ones NOT already surfaced by the dedicated zero-image banner (issue #116), so
 * the two amber banners never duplicate the same "images exhausted" note.
 */
describe("nonImageWarnings", () => {
  const IMAGE_WARNING =
    "Step '_images_generated' exhausted retries and produced no output."
  const TREND_WARNING =
    "Step 'gs_web_search_insights' exhausted retries and produced no output."
  const CAMPAIGN_WARNING =
    "Step 'campaign_web_search_insights' exhausted retries and produced no output."

  it("returns [] for undefined", () => {
    expect(nonImageWarnings(undefined)).toEqual([])
  })

  it("returns [] for an empty list", () => {
    expect(nonImageWarnings([])).toEqual([])
  })

  it("drops the image-exhaustion warning (covered by the zero-image banner)", () => {
    expect(nonImageWarnings([IMAGE_WARNING])).toEqual([])
  })

  it("keeps research-producer warnings", () => {
    expect(nonImageWarnings([TREND_WARNING, CAMPAIGN_WARNING])).toEqual([
      TREND_WARNING,
      CAMPAIGN_WARNING,
    ])
  })

  it("filters only the image warning out of a mixed list", () => {
    expect(
      nonImageWarnings([TREND_WARNING, IMAGE_WARNING, CAMPAIGN_WARNING]),
    ).toEqual([TREND_WARNING, CAMPAIGN_WARNING])
  })
})
