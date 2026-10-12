import { describe, expect, it } from "vitest"

import {
  buildProofs,
  conceptNameToFilename,
  DEFAULT_PASS_THRESHOLD,
  findAdCopyEval,
  findAdCopyForVisual,
  matchAdCopy,
  findVisualEval,
  passThreshold,
  proofScore,
  sortProofs,
  type AdCopy,
  type AdCopyEvaluation,
  type CreativeScore,
  type EvalReport,
  type Proof,
  type VisualConcept,
} from "@/lib/eval-matching"

function score(overall: number): CreativeScore {
  return {
    overall_score: overall,
    passed: overall >= 0.7,
    verdicts: [],
    strengths: [],
    improvements: [],
  }
}

function acEval(id: number, headline: string, overall = 0.8): AdCopyEvaluation {
  return { original_id: id, headline, tone_style: "Playful", score: score(overall) }
}

function adCopy(id: number, headline: string): AdCopy {
  return {
    original_id: id,
    headline,
    body_text: "",
    tone_style: "",
    trend_connection: "",
    audience_appeal_rationale: "",
    social_caption: "",
    call_to_action: "",
    detailed_performance_rationale: "",
  }
}

function concept(adCopyId: number, headline: string, name = headline): VisualConcept {
  return {
    ad_copy_id: adCopyId,
    concept_name: name,
    trend: "",
    trend_reference: "",
    markets_product: "",
    audience_appeal: "",
    selection_rationale: "",
    headline,
    social_caption: "",
    call_to_action: "",
    concept_summary: "",
    image_generation_prompt: "",
  }
}

function report(
  adEvals: AdCopyEvaluation[],
  visualEvals: EvalReport["visual_concept_evaluations"] = []
): EvalReport {
  return {
    brand: "",
    target_product: "",
    target_search_trend: "",
    ad_copy_evaluations: adEvals,
    visual_concept_evaluations: visualEvals,
    summary: {
      total_ad_copies: 0,
      ad_copies_passed: 0,
      avg_ad_copy_score: 0,
      total_visual_concepts: 0,
      visual_concepts_passed: 0,
      avg_visual_score: 0,
      overall_pass_rate: 0,
      weakest_dimensions: [],
    },
  }
}

describe("conceptNameToFilename", () => {
  it("replaces spaces with underscores and appends .png", () => {
    expect(conceptNameToFilename("The Jackpot Reveal")).toBe("The_Jackpot_Reveal.png")
  })

  it("strips punctuation like Python's REMOVE_PUNCTUATION", () => {
    expect(conceptNameToFilename("Signs, Signs & More: Signs!")).toBe(
      "Signs_Signs__More_Signs.png"
    )
    expect(conceptNameToFilename("Rock'n'Roll (Live)")).toBe("RocknRoll_Live.png")
  })

  it("keeps underscores and digits", () => {
    expect(conceptNameToFilename("v2_final 1.8B")).toBe("v2_final_18B.png")
  })
})

describe("findVisualEval", () => {
  const r = report([], [
    { ad_copy_id: 1, concept_name: "Alpha", score: score(0.9) },
    { ad_copy_id: 2, concept_name: "Beta", score: score(0.5) },
  ])

  it("matches by exact concept name", () => {
    expect(findVisualEval(r, "Beta")?.ad_copy_id).toBe(2)
  })

  it("returns undefined without a match or report", () => {
    expect(findVisualEval(r, "beta")).toBeUndefined()
    expect(findVisualEval(null, "Alpha")).toBeUndefined()
  })
})

describe("matchAdCopy", () => {
  const copies = [adCopy(1, "One"), adCopy(2, "Two")]

  it("reports an id match first, even if the headline points elsewhere", () => {
    expect(matchAdCopy(copies, concept(2, "One"), 0)).toEqual({ item: copies[1], via: "id" })
  })

  it("reports a headline match when the id doesn't match", () => {
    expect(matchAdCopy(copies, concept(9, "Two"), 0)).toEqual({ item: copies[1], via: "headline" })
  })

  it("reports an index match when neither id nor headline match", () => {
    expect(matchAdCopy(copies, concept(9, "?"), 0)).toEqual({ item: copies[0], via: "index" })
  })

  it("returns null past the end or with no copies", () => {
    expect(matchAdCopy(copies, concept(9, "?"), 2)).toBeNull()
    expect(matchAdCopy([], concept(1, "One"), 0)).toBeNull()
  })
})

describe("findAdCopyEval", () => {
  const evals = [acEval(10, "First"), acEval(20, "Second"), acEval(30, "Third")]
  const r = report(evals)

  it("matches the copy's eval by id, never by a differing headline", () => {
    expect(findAdCopyEval(r, { original_id: 20, headline: "First" })).toBe(evals[1])
    expect(findAdCopyEval(r, { original_id: 99, headline: "Third" })).toBeUndefined()
  })

  it("falls back to the headline only when the eval has no id", () => {
    const idless = { ...acEval(0, "Second"), original_id: undefined as unknown as number }
    expect(findAdCopyEval(report([idless]), { original_id: 20, headline: "Second" })).toBe(idless)
    expect(findAdCopyEval(report([idless]), { original_id: 20, headline: "Other" })).toBeUndefined()
  })

  it("returns undefined with no evals or no report", () => {
    expect(findAdCopyEval(report([]), { original_id: 10, headline: "First" })).toBeUndefined()
    expect(findAdCopyEval(null, { original_id: 10, headline: "First" })).toBeUndefined()
  })
})

describe("findAdCopyForVisual", () => {
  const copies = [adCopy(1, "One"), adCopy(2, "Two")]

  it("uses the same id → headline → index order", () => {
    expect(findAdCopyForVisual(copies, concept(2, "One"), 0)).toBe(copies[1])
    expect(findAdCopyForVisual(copies, concept(9, "Two"), 0)).toBe(copies[1])
    expect(findAdCopyForVisual(copies, concept(9, "?"), 0)).toBe(copies[0])
    expect(findAdCopyForVisual(copies, concept(9, "?"), 2)).toBeUndefined()
    expect(findAdCopyForVisual([], concept(1, "One"), 0)).toBeUndefined()
  })
})

describe("buildProofs", () => {
  it("pairs every concept with its ad copy and both evals", () => {
    const concepts = [concept(1, "One", "Alpha"), concept(2, "Two", "Beta")]
    const r = report([acEval(1, "One", 0.9), acEval(2, "Two", 0.6)], [
      { ad_copy_id: 2, concept_name: "Beta", score: score(0.4) },
    ])
    const proofs = buildProofs(concepts, [adCopy(1, "One"), adCopy(2, "Two")], r)
    expect(proofs.map((p) => p.index)).toEqual([0, 1])
    expect(proofs[0].adCopy?.original_id).toBe(1)
    expect(proofs[0].adCopyMatch).toBe("id")
    expect(proofs[0].adCopyEval?.original_id).toBe(1)
    expect(proofs[0].visualEval).toBeUndefined()
    expect(proofs[1].visualEval?.concept_name).toBe("Beta")
  })

  it("leaves evals undefined when the report is missing", () => {
    const [p] = buildProofs([concept(1, "One")], [], null)
    expect(p.adCopyEval).toBeUndefined()
    expect(p.visualEval).toBeUndefined()
    expect(p.adCopy).toBeUndefined()
    expect(p.adCopyMatch).toBeNull()
  })

  it("still shows an index-matched copy, flagged, with that copy's own eval", () => {
    // Neither id 99 nor "Nope" matches: the copy falls back to position 0 (id 2).
    // A positional eval lookup would borrow evals[0] (id 1), another copy's verdict.
    const copies = [adCopy(2, "Two"), adCopy(1, "One")]
    const r = report([acEval(1, "One", 0.9), acEval(2, "Two", 0.3)])
    const [p] = buildProofs([concept(99, "Nope")], copies, r)
    expect(p.adCopy).toBe(copies[0])
    expect(p.adCopyMatch).toBe("index")
    expect(p.adCopyEval?.original_id).toBe(2)
    expect(p.adCopyEval?.score.overall_score).toBe(0.3)
  })

  it("shows no copy eval when the found copy has none (no positional borrowing)", () => {
    const [p] = buildProofs(
      [concept(99, "Nope")],
      [adCopy(5, "Five")],
      report([acEval(1, "One"), acEval(2, "Two")])
    )
    expect(p.adCopy?.original_id).toBe(5)
    expect(p.adCopyEval).toBeUndefined()
  })

  it("pairs the eval from a headline-matched copy by that copy's id", () => {
    const [p] = buildProofs(
      [concept(99, "Two")],
      [adCopy(1, "One"), adCopy(2, "Two")],
      report([acEval(1, "One"), acEval(2, "Two")])
    )
    expect(p.adCopyMatch).toBe("headline")
    expect(p.adCopyEval?.original_id).toBe(2)
  })

  it("without session copies, matches the eval by the concept's id or headline, never by index", () => {
    const r = report([acEval(1, "One"), acEval(2, "Two")])
    expect(buildProofs([concept(2, "x")], [], r)[0].adCopyEval?.original_id).toBe(2)
    expect(buildProofs([concept(99, "Nope")], [], r)[0].adCopyEval).toBeUndefined()
  })
})

describe("proofScore + sortProofs", () => {
  const mk = (index: number, ac?: number, vis?: number): Proof => ({
    index,
    concept: concept(index, `h${index}`),
    adCopyMatch: null,
    adCopyEval: ac === undefined ? undefined : acEval(index, `h${index}`, ac),
    visualEval:
      vis === undefined
        ? undefined
        : { ad_copy_id: index, concept_name: `h${index}`, score: score(vis) },
  })
  const proofs = [mk(0, 0.8, 0.6), mk(1), mk(2, 0.9, 0.9), mk(3, 0.5)]

  it("averages the available scores, null when unevaluated", () => {
    expect(proofScore(proofs[0])).toBeCloseTo(0.7)
    expect(proofScore(proofs[1])).toBeNull()
    expect(proofScore(proofs[3])).toBe(0.5)
  })

  it("sorts by pipeline order, highest, lowest (unevaluated last)", () => {
    const shuffled = [proofs[2], proofs[0], proofs[3], proofs[1]]
    expect(sortProofs(shuffled, "pipeline").map((p) => p.index)).toEqual([0, 1, 2, 3])
    expect(sortProofs(proofs, "highest").map((p) => p.index)).toEqual([2, 0, 3, 1])
    expect(sortProofs(proofs, "lowest").map((p) => p.index)).toEqual([3, 0, 2, 1])
  })

  it("does not mutate the input", () => {
    const copy = [...proofs]
    sortProofs(proofs, "highest")
    expect(proofs).toEqual(copy)
  })
})

describe("passThreshold", () => {
  it("uses the report's threshold when present, else the default", () => {
    expect(passThreshold({ passing_threshold: 0.6 })).toBe(0.6)
    expect(passThreshold({})).toBe(DEFAULT_PASS_THRESHOLD)
    expect(passThreshold(null)).toBe(0.7)
    expect(passThreshold({ passing_threshold: 7 })).toBe(0.7)
  })
})
