import { describe, expect, it } from "vitest"

import {
  buildProofs,
  conceptNameToFilename,
  DEFAULT_PASS_THRESHOLD,
  findAdCopyEvalForVisual,
  findAdCopyForVisual,
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

describe("findAdCopyEvalForVisual", () => {
  const evals = [acEval(10, "First"), acEval(20, "Second"), acEval(30, "Third")]
  const r = report(evals)

  it("matches by ad_copy_id first, even if the headline points elsewhere", () => {
    expect(findAdCopyEvalForVisual(r, concept(20, "First"), 0)).toBe(evals[1])
  })

  it("falls back to headline when the id doesn't match", () => {
    expect(findAdCopyEvalForVisual(r, concept(99, "Third"), 0)).toBe(evals[2])
  })

  it("falls back to index position when neither id nor headline match", () => {
    expect(findAdCopyEvalForVisual(r, concept(99, "Nope"), 1)).toBe(evals[1])
  })

  it("returns undefined past the end, with no evals, or with no report", () => {
    expect(findAdCopyEvalForVisual(r, concept(99, "Nope"), 3)).toBeUndefined()
    expect(findAdCopyEvalForVisual(report([]), concept(1, "x"), 0)).toBeUndefined()
    expect(findAdCopyEvalForVisual(null, concept(10, "First"), 0)).toBeUndefined()
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
    expect(proofs[0].adCopyEval?.original_id).toBe(1)
    expect(proofs[0].visualEval).toBeUndefined()
    expect(proofs[1].visualEval?.concept_name).toBe("Beta")
  })

  it("leaves evals undefined when the report is missing", () => {
    const [p] = buildProofs([concept(1, "One")], [], null)
    expect(p.adCopyEval).toBeUndefined()
    expect(p.visualEval).toBeUndefined()
    expect(p.adCopy).toBeUndefined()
  })
})

describe("proofScore + sortProofs", () => {
  const mk = (index: number, ac?: number, vis?: number): Proof => ({
    index,
    concept: concept(index, `h${index}`),
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
