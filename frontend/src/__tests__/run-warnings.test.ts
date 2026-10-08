import { describe, expect, it } from "vitest"

import { classifyWarnings } from "@/lib/run-warnings"

/**
 * `classifyWarnings` splits the eval report's run notes (from
 * `collect_degradation_warnings` + the judge) into research gaps, failed saves
 * and quality flags, so a quality note never shows under the research banner.
 */
describe("classifyWarnings", () => {
  const exhausted = (key: string) =>
    `Step '${key}' exhausted retries and produced no output.`

  it("returns empty groups for undefined or []", () => {
    const empty = { research: [], saves: [], quality: [] }
    expect(classifyWarnings(undefined)).toEqual(empty)
    expect(classifyWarnings([])).toEqual(empty)
  })

  it("puts research and brief exhaustions under research", () => {
    const notes = [
      exhausted("gs_web_search_insights"),
      exhausted("campaign_web_search_insights"),
      exhausted("refined_web_search_insights"),
      exhausted("creative_brief"),
      exhausted("info_gtrends"),
    ]
    expect(classifyWarnings(notes)).toEqual({ research: notes, saves: [], quality: [] })
  })

  it("drops the image exhaustion (it has its own zero-image banner)", () => {
    expect(classifyWarnings([exhausted("_images_generated")])).toEqual({
      research: [],
      saves: [],
      quality: [],
    })
  })

  it("puts persistence failures under saves (new and legacy labels)", () => {
    const notes = [
      "Research PDF save has unresolved issues: 1 (e.g. research PDF failed: OSError: disk)",
      "Eval report save has unresolved issues: 1 (e.g. eval report (GCS) failed: x)",
      "Gallery save has unresolved issues: 1 (e.g. HTML gallery failed: x)",
      "Trend row save has unresolved issues: 1 (e.g. creative row (BigQuery) failed: x)",
      "Eval row save has unresolved issues: 1 (e.g. eval row (BigQuery) failed: x)",
      // Reports written before the labels were humanized.
      "Research report gcs uri has unresolved issues: 1 (e.g. research PDF failed: x)",
      "Eval report gcs uri has unresolved issues: 1 (e.g. x)",
      "Creative gallery gcs uri has unresolved issues: 1 (e.g. x)",
      "Creative row uuid has unresolved issues: 1 (e.g. x)",
      "Eval bq row uuid has unresolved issues: 1 (e.g. x)",
    ]
    expect(classifyWarnings(notes)).toEqual({ research: [], saves: notes, quality: [] })
  })

  it("puts the image-check note under quality (old and new label)", () => {
    const notes = [
      "Image check has unresolved issues: 1 (e.g. The Lonely Tech Apron: WWE logo on road case sticker)",
      "Image qa has unresolved issues: 1 (e.g. The Lonely Tech Apron: WWE logo on road case sticker)",
    ]
    expect(classifyWarnings(notes)).toEqual({ research: [], saves: [], quality: notes })
  })

  it("puts copy/concept/brief issues and judge notes under quality", () => {
    const notes = [
      "Ad copy check has unresolved issues: 1 (e.g. Copy 2: CTA too long)",
      "Visual concept check has unresolved issues: 2 (e.g. Jackpot: missing motif)",
      "Creative brief has unresolved issues: 1 (e.g. insight has no tension)",
      "3 checks not reported by the judge (passed as not checked)",
      "Visual judge could not read the rendered image and judged the prompt instead for: Jackpot",
      exhausted("creative_evaluation_report"),
    ]
    expect(classifyWarnings(notes)).toEqual({ research: [], saves: [], quality: notes })
  })

  it("splits a mixed list, keeping order within each group", () => {
    const research = exhausted("gs_web_search_insights")
    const save = "Gallery save has unresolved issues: 1 (e.g. x)"
    const quality = "Image check has unresolved issues: 1 (e.g. y)"
    expect(
      classifyWarnings([quality, research, exhausted("_images_generated"), save]),
    ).toEqual({ research: [research], saves: [save], quality: [quality] })
  })
})
