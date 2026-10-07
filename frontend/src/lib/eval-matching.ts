/**
 * Creative evaluation report types and the pure logic that pairs each visual
 * concept (session state) with its ad copy and its two eval verdicts.
 *
 * Matching rules (unchanged from the original results page):
 * - visual eval: by exact `concept_name`
 * - ad copy eval / ad copy: by `ad_copy_id` → `original_id`, then by headline,
 *   then by index position
 */

export interface EvalVerdict {
  dimension: string;
  score: number;
  verdict: "pass" | "fail";
  rationale: string;
}

/**
 * One binary compliance check ("gate") from the judge. `advisory` gates are
 * recorded but never fail a creative. Absent on reports written before gates.
 */
export interface GateResult {
  gate: string;
  passed: boolean;
  note?: string;
  advisory?: boolean;
}

export interface CreativeScore {
  overall_score: number;
  /** Score ≥ threshold AND (on gated reports) every non-advisory gate passed. */
  passed: boolean;
  verdicts: EvalVerdict[];
  strengths: string[];
  improvements: string[];
  /** Binary compliance checks (absent on older reports). */
  gates?: GateResult[];
  /** Every non-advisory gate passed (absent on older reports). */
  gates_passed?: boolean;
}

export interface AdCopyEvaluation {
  original_id: number;
  headline: string;
  tone_style: string;
  score: CreativeScore;
}

export interface VisualConceptEvaluation {
  ad_copy_id: number;
  concept_name: string;
  score: CreativeScore;
  /** True when the judge saw the rendered image; false = the prompt only. */
  image_judged?: boolean;
}

export interface EvalReport {
  brand: string;
  target_product: string;
  target_search_trend: string;
  ad_copy_evaluations: AdCopyEvaluation[];
  visual_concept_evaluations: VisualConceptEvaluation[];
  /** Degradation notes from retry-exhausted pipeline steps (may be absent on older reports). */
  warnings?: string[];
  /** Pass threshold (0–1); absent on reports written before it was recorded. */
  passing_threshold?: number;
  /** True when the judge saw the structured creative brief. */
  brief_used?: boolean;
  summary: {
    total_ad_copies: number;
    ad_copies_passed: number;
    avg_ad_copy_score: number;
    total_visual_concepts: number;
    visual_concepts_passed: number;
    avg_visual_score: number;
    overall_pass_rate: number;
    weakest_dimensions: string[];
    /** Share of creatives whose gates all passed (null/absent on older reports). */
    gates_pass_rate?: number | null;
  };
}

/** creative_eval's default `passing_threshold` (creative_eval/config.py). */
export const DEFAULT_PASS_THRESHOLD = 0.7;

/** The report's pass threshold, or the creative_eval default when absent/invalid. */
export function passThreshold(
  report: Pick<EvalReport, "passing_threshold"> | null | undefined
): number {
  const t = report?.passing_threshold;
  return typeof t === "number" && t > 0 && t <= 1 ? t : DEFAULT_PASS_THRESHOLD;
}

/** The gates of a score, dropping malformed entries ([] on older reports). */
export function scoreGates(score: CreativeScore | null | undefined): GateResult[] {
  const gates = score?.gates;
  if (!Array.isArray(gates)) return [];
  return gates.filter(
    (g): g is GateResult =>
      !!g && typeof g === "object" && typeof g.gate === "string" && typeof g.passed === "boolean"
  );
}

/** The failed gates that block a pass (non-advisory). */
export function failedGates(score: CreativeScore | null | undefined): GateResult[] {
  return scoreGates(score).filter((g) => !g.passed && !g.advisory);
}

/** Visual concept data from session state (`final_visual_concepts`). */
export interface VisualConcept {
  ad_copy_id: number;
  concept_name: string;
  trend: string;
  trend_reference: string;
  markets_product: string;
  audience_appeal: string;
  selection_rationale: string;
  headline: string;
  social_caption: string;
  call_to_action: string;
  concept_summary: string;
  image_generation_prompt: string;
  visual_style?: string;
}

/** Ad copy data from session state (`ad_copy_critique`). */
export interface AdCopy {
  original_id: number;
  headline: string;
  body_text: string;
  tone_style: string;
  trend_connection: string;
  audience_appeal_rationale: string;
  social_caption: string;
  call_to_action: string;
  detailed_performance_rationale: string;
}

/** Replicate Python's REMOVE_PUNCTUATION + replace(" ", "_") for image filenames. */
export function conceptNameToFilename(name: string): string {
  return name.replace(/[^\w\s]/g, "").replace(/ /g, "_") + ".png";
}

/** Find the visual eval for a concept (exact concept_name match). */
export function findVisualEval(
  report: EvalReport | null | undefined,
  conceptName: string
): VisualConceptEvaluation | undefined {
  return report?.visual_concept_evaluations.find(
    (ve) => ve.concept_name === conceptName
  );
}

type ConceptKey = Pick<VisualConcept, "ad_copy_id" | "headline">;

/** id → headline → index fallback shared by ad copy and ad copy eval lookup. */
function matchByIdHeadlineIndex<T extends { original_id: number; headline: string }>(
  items: T[],
  vc: ConceptKey,
  vcIndex: number
): T | undefined {
  // 1. Match by ID
  const byId = items.find((it) => it.original_id === vc.ad_copy_id);
  if (byId) return byId;
  // 2. Match by headline (visual concept carries the ad copy headline)
  const byHeadline = items.find((it) => it.headline === vc.headline);
  if (byHeadline) return byHeadline;
  // 3. Fall back to index position
  if (vcIndex < items.length) return items[vcIndex];
  return undefined;
}

/** Find the ad copy eval for a visual concept: by id, then headline, then index. */
export function findAdCopyEvalForVisual(
  report: EvalReport | null | undefined,
  vc: ConceptKey,
  vcIndex: number
): AdCopyEvaluation | undefined {
  if (!report) return undefined;
  return matchByIdHeadlineIndex(report.ad_copy_evaluations, vc, vcIndex);
}

/** Find the session-state ad copy for a visual concept: by id, then headline, then index. */
export function findAdCopyForVisual(
  adCopies: AdCopy[],
  vc: ConceptKey,
  vcIndex: number
): AdCopy | undefined {
  return matchByIdHeadlineIndex(adCopies, vc, vcIndex);
}

/**
 * The post-render image check for one concept, from session state
 * `generated_images[concept_name]` (written by `generate_image`): `qa` is the
 * vision model's verdict plus the backend rule's `passed`/`failures`;
 * `attempts` counts renders (2 = re-rendered once). `qa` is null when image QA
 * was disabled or unavailable — no check to show.
 */
export interface ImageCheck {
  passed: boolean;
  /** Short issues for a failed check (empty when passed). */
  issues: string[];
  /** Re-renders after the first attempt (0 when the first render was kept). */
  rerenders: number;
}

/** Parse `generated_images[conceptName]` into an ImageCheck, or undefined. */
export function imageCheckFor(
  generatedImages: unknown,
  conceptName: string
): ImageCheck | undefined {
  if (!generatedImages || typeof generatedImages !== "object") return undefined;
  const record = (generatedImages as Record<string, unknown>)[conceptName];
  if (!record || typeof record !== "object") return undefined;
  const { qa, attempts } = record as { qa?: unknown; attempts?: unknown };
  if (!qa || typeof qa !== "object") return undefined;
  const { passed, failures } = qa as { passed?: unknown; failures?: unknown };
  if (typeof passed !== "boolean") return undefined;
  const issues = Array.isArray(failures)
    ? failures.filter((f): f is string => typeof f === "string" && f.trim() !== "")
    : [];
  const renders = typeof attempts === "number" && Number.isFinite(attempts) ? attempts : 1;
  return { passed, issues: passed ? [] : issues, rerenders: Math.max(0, Math.floor(renders) - 1) };
}

/** One creative on the contact sheet: concept + its ad copy + both evals. */
export interface Proof {
  /** Pipeline position (index into final_visual_concepts). */
  index: number;
  concept: VisualConcept;
  adCopy?: AdCopy;
  adCopyEval?: AdCopyEvaluation;
  visualEval?: VisualConceptEvaluation;
  /** Post-render image check (absent when QA was off/unavailable). */
  imageCheck?: ImageCheck;
}

export function buildProofs(
  concepts: VisualConcept[],
  adCopies: AdCopy[],
  report: EvalReport | null | undefined,
  generatedImages?: unknown
): Proof[] {
  return concepts.map((concept, index) => ({
    index,
    concept,
    adCopy: findAdCopyForVisual(adCopies, concept, index),
    adCopyEval: findAdCopyEvalForVisual(report, concept, index),
    visualEval: findVisualEval(report, concept.concept_name),
    imageCheck: imageCheckFor(generatedImages, concept.concept_name),
  }));
}

/** Mean of the available overall scores (0–1), or null when unevaluated. */
export function proofScore(p: Proof): number | null {
  const scores = [
    p.adCopyEval?.score.overall_score,
    p.visualEval?.score.overall_score,
  ].filter((s): s is number => typeof s === "number");
  if (scores.length === 0) return null;
  return scores.reduce((a, b) => a + b, 0) / scores.length;
}

/** Blocking failed checks across a proof's ad copy and visual evals. */
export function proofFailedChecks(p: Proof): GateResult[] {
  return [...failedGates(p.adCopyEval?.score), ...failedGates(p.visualEval?.score)];
}

export type ProofSort = "pipeline" | "highest" | "lowest";

/** Sort proofs; unevaluated proofs always go last, ties keep pipeline order. */
export function sortProofs(proofs: Proof[], mode: ProofSort): Proof[] {
  if (mode === "pipeline") return [...proofs].sort((a, b) => a.index - b.index);
  const dir = mode === "highest" ? -1 : 1;
  return [...proofs].sort((a, b) => {
    const sa = proofScore(a);
    const sb = proofScore(b);
    if (sa === null && sb === null) return a.index - b.index;
    if (sa === null) return 1;
    if (sb === null) return -1;
    return sa === sb ? a.index - b.index : (sa - sb) * dir;
  });
}
