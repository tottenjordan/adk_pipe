/**
 * Creative evaluation report types and the pure logic that pairs each visual
 * concept (session state) with its ad copy and its two eval verdicts.
 *
 * Matching rules (unchanged from the original results page):
 * - visual eval: by exact `concept_name`
 * - ad copy: by `ad_copy_id` → `original_id`, then by headline, then by index
 *   position; how it was found is kept on the proof (`adCopyMatch`), and only an
 *   id or headline match is reliable enough to rate (`lib/ratings.ts`)
 * - ad copy eval: from the copy actually found (its `original_id`; the headline
 *   only for an eval without an id), so a copy and its verdict always belong
 *   together; without session copies, by the concept's id/headline (never index)
 */

import { formatValidUntil, pairedCopy, timelyStatusFor } from "@/lib/timely";

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
  /** True when opt-in rating-driven learning steered this run (absent on older reports). */
  learning_used?: boolean;
  /** What learning changed: "guidance", "styles" and each rating strictness flag. */
  learning_flags?: string[];
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
  /** True when the concept asked to cast the run's consented person. */
  casts_person_reference?: boolean;
  /** One sentence on why the concept casts (or would not cast) the person. */
  person_casting_reason?: string;
  /** True for the timely creative that references the validated current moment. */
  uses_trend_state?: boolean;
  /** One sentence on how the creative references the current moment. */
  timely_reason?: string;
}

/** A timely proof: why it references the current moment, until when, and whether that has passed. */
export interface Timely {
  reason: string;
  /** "Oct 14, 23:59" from `state.trend_situation.valid_until` (its own offset), or null. */
  validUntil: string | null;
  /** `trend_situation.valid_until` as written. */
  validUntilIso: string;
  /** The moment has passed (`valid_until` <= now); never for an unparseable one. */
  expired: boolean;
}

/**
 * The proof's timely slot (lib/timely.ts rule): the concept's or its paired
 * copy's `uses_trend_state` flag (set only when the run had a validated timely
 * moment) and the run's `trend_situation` (`timely_ok` + `valid_until`).
 * `now` is the wall clock in ms (0 = not known yet, never expired). The copy
 * pairs by id only, like the api's guards. Undefined for evergreen concepts.
 */
export function timelyFor(
  concept: VisualConcept,
  trendSituation: unknown,
  adCopies: readonly AdCopy[] = [],
  now = 0,
): Timely | undefined {
  const copy = pairedCopy(adCopies, concept);
  const status = timelyStatusFor(concept, copy, trendSituation, now);
  if (!status.timely) return undefined;
  const own = typeof concept.timely_reason === "string" ? concept.timely_reason.trim() : "";
  const copyReason = typeof copy?.timely_reason === "string" ? copy.timely_reason.trim() : "";
  return {
    reason: own || copyReason,
    validUntil: formatValidUntil(status.validUntilIso),
    validUntilIso: status.validUntilIso,
    expired: status.expired,
  };
}

/** Person casting of one proof: a cast render, or a person photo rejected at render time. */
export interface Casting {
  cast: boolean;
  reason: string;
  /** `state.person_reference_rejected[concept]` (e.g. "image_safety", "photo_unavailable"). */
  rejected?: string;
}

/**
 * The proof's casting, from the render record (`generated_images[concept].cast`,
 * set only on runs with a person reference) and `person_reference_rejected`.
 * Undefined for uncast concepts and runs without a person.
 */
export function castingFor(
  concept: VisualConcept,
  generatedImages: unknown,
  personRejected: unknown,
): Casting | undefined {
  const reason = typeof concept.person_casting_reason === "string" ? concept.person_casting_reason.trim() : "";
  const rejected =
    personRejected && typeof personRejected === "object"
      ? (personRejected as Record<string, unknown>)[concept.concept_name]
      : undefined;
  if (typeof rejected === "string" && rejected) return { cast: false, reason, rejected };
  const record =
    generatedImages && typeof generatedImages === "object"
      ? (generatedImages as Record<string, unknown>)[concept.concept_name]
      : undefined;
  if (record && typeof record === "object" && (record as { cast?: unknown }).cast === true) {
    return { cast: true, reason };
  }
  return undefined;
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
  /** True for the timely copy that references the validated current moment. */
  uses_trend_state?: boolean;
  /** One sentence on how the copy references the current moment. */
  timely_reason?: string;
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
type CopyKey = Pick<AdCopy, "original_id" | "headline">;

/**
 * How a concept's ad copy was found. `"index"` is a blind positional guess
 * (neither id nor headline matched): fine to display, never to rate.
 */
export type AdCopyMatchVia = "id" | "headline" | "index";

export interface AdCopyMatch<T> {
  item: T;
  via: AdCopyMatchVia;
}

/** A concept's ad copy: by id, then headline, then index position, with how it matched. */
export function matchAdCopy<T extends CopyKey>(
  items: T[],
  vc: ConceptKey,
  vcIndex: number
): AdCopyMatch<T> | null {
  const byId = items.find((it) => it.original_id === vc.ad_copy_id);
  if (byId) return { item: byId, via: "id" };
  // The visual concept carries its ad copy's headline.
  const byHeadline = items.find((it) => it.headline === vc.headline);
  if (byHeadline) return { item: byHeadline, via: "headline" };
  if (vcIndex < items.length) return { item: items[vcIndex], via: "index" };
  return null;
}

/** The session-state ad copy for a visual concept (id → headline → index), without provenance. */
export function findAdCopyForVisual(
  adCopies: AdCopy[],
  vc: ConceptKey,
  vcIndex: number
): AdCopy | undefined {
  return matchAdCopy(adCopies, vc, vcIndex)?.item;
}

function hasId(id: unknown): boolean {
  return (typeof id === "number" && Number.isFinite(id)) || (typeof id === "string" && id.trim() !== "");
}

/**
 * The eval of one specific ad copy: by its `original_id`, falling back to the
 * headline only when the eval (or the copy) has no id. Never positional, so the
 * verdict shown always belongs to the copy shown.
 */
export function findAdCopyEval(
  report: EvalReport | null | undefined,
  copy: CopyKey
): AdCopyEvaluation | undefined {
  const evals = report?.ad_copy_evaluations;
  if (!Array.isArray(evals)) return undefined;
  return evals.find((ev) =>
    hasId(ev.original_id) && hasId(copy.original_id)
      ? ev.original_id === copy.original_id
      : ev.headline === copy.headline
  );
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
  /** How `adCopy` was found (null = no copy). Only "id"/"headline" may be rated. */
  adCopyMatch: AdCopyMatchVia | null;
  /** The eval of `adCopy` itself (never borrowed from another position). */
  adCopyEval?: AdCopyEvaluation;
  visualEval?: VisualConceptEvaluation;
  /** Post-render image check (absent when QA was off/unavailable). */
  imageCheck?: ImageCheck;
  /** Person casting (absent for uncast concepts and runs without a person). */
  casting?: Casting;
  /** The timely slot (absent for evergreen concepts). */
  timely?: Timely;
}

export function buildProofs(
  concepts: VisualConcept[],
  adCopies: AdCopy[],
  report: EvalReport | null | undefined,
  generatedImages?: unknown,
  personRejected?: unknown,
  trendSituation?: unknown,
  /** Wall clock (ms) for the timely expiry; 0 = not known yet (never expired). */
  now = 0
): Proof[] {
  return concepts.map((concept, index) => {
    const match = matchAdCopy(adCopies, concept, index);
    return {
      index,
      concept,
      adCopy: match?.item,
      adCopyMatch: match?.via ?? null,
      // Pair the verdict from the copy actually shown; with no session copy at
      // all, the concept's own id/headline still names its copy reliably.
      adCopyEval: findAdCopyEval(
        report,
        match?.item ?? { original_id: concept.ad_copy_id, headline: concept.headline }
      ),
      visualEval: findVisualEval(report, concept.concept_name),
      imageCheck: imageCheckFor(generatedImages, concept.concept_name),
      casting: castingFor(concept, generatedImages, personRejected),
      timely: timelyFor(concept, trendSituation, adCopies, now),
    };
  });
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
