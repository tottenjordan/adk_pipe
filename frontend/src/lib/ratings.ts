/**
 * Human creative ratings (judge calibration): types and pure helpers for the
 * results-page rating control. The REST client lives in `lib/api.ts`
 * (`putRating` / `getRatings`); the backend is `runserver/ratings.py`.
 */
import type { Proof } from "./eval-matching";
import { failReasonsFor, type FailReason } from "./rating-reasons";

export type RatingKind = "visual" | "ad_copy";
export type RatingVerdict = "pass" | "fail";

/** A stored rating as `GET /ratings/{user}/{session}` returns it. */
export interface Rating {
  rating_id?: string;
  session_id?: string;
  app_name?: string;
  creative_key: string;
  kind: RatingKind;
  verdict: RatingVerdict;
  score: number | null;
  note: string | null;
  judge_overall?: number | null;
  judge_passed?: boolean | null;
  judge_gates_passed?: boolean | null;
  judge_model?: string | null;
  /** Where the judge fields came from: the run's GCS report, session state, or none. */
  judge_source?: "gcs" | "state" | "none" | null;
  /** creative_eval JUDGE_VERSION of the run's report ("" = an earlier, unversioned judge). */
  judge_version?: string | null;
  /** The run was steered by opt-in rating learning (report `learning_used`). */
  learning_used?: boolean | null;
  /** Allowlisted fail-reason chips (`lib/rating-reasons.ts`); empty on a pass. */
  fail_reasons?: string[] | null;
  created_at?: string;
  updated_at?: string;
}

/** The `PUT /ratings/{user}/{session}` body. */
export interface RatingPayload {
  app_name: string;
  creative_key: string;
  kind: RatingKind;
  verdict: RatingVerdict;
  score: number | null;
  note: string | null;
  fail_reasons?: FailReason[];
}

/** What the control edits before saving (`verdict` empty until chosen). */
export interface RatingDraft {
  verdict: RatingVerdict | "";
  score: number | null;
  note: string;
  /** Fail-reason chips; only sent with a fail verdict. */
  failReasons: FailReason[];
}

export const NOTE_MAX_CHARS = 2000;

/** The visual concept's key, and its paired ad copy's key when it has one. */
export function creativeKeysFor(proof: Proof): { visual: string; adCopy?: string } {
  const out: { visual: string; adCopy?: string } = {
    visual: `visual:${proof.concept.concept_name}`,
  };
  const id = proof.adCopy?.original_id;
  if (id !== undefined && id !== null && String(id).trim() !== "") {
    out.adCopy = `copy:${id}`;
  }
  return out;
}

/**
 * The saved reasons the control offers for this rating's kind (enum values the
 * chips don't show are dropped, so a hidden reason is never re-sent).
 */
function offeredReasons(rating?: Rating): FailReason[] {
  if (!rating || (rating.kind !== "visual" && rating.kind !== "ad_copy")) return [];
  const offered: readonly string[] = failReasonsFor(rating.kind);
  const saved = (rating.fail_reasons ?? []).filter((r): r is FailReason => offered.includes(r));
  return [...new Set(saved)];
}

export function draftFrom(rating?: Rating): RatingDraft {
  return {
    verdict: rating?.verdict ?? "",
    score: rating?.score ?? null,
    note: rating?.note ?? "",
    failReasons: offeredReasons(rating),
  };
}

function sameReasons(a: readonly FailReason[], b: readonly FailReason[]): boolean {
  const set = new Set(a);
  return set.size === new Set(b).size && b.every((r) => set.has(r));
}

/** True when the draft would change the saved rating (or there is none yet). */
export function isDirty(draft: RatingDraft, saved?: Rating): boolean {
  const base = draftFrom(saved);
  return (
    draft.verdict !== base.verdict ||
    draft.score !== base.score ||
    draft.note.trim() !== base.note.trim() ||
    !sameReasons(draft.failReasons, base.failReasons)
  );
}

/** The PUT body, or null while the draft can't be saved (no verdict, bad score, long note). */
export function buildRatingPayload(
  appName: string,
  creativeKey: string,
  kind: RatingKind,
  draft: RatingDraft
): RatingPayload | null {
  if (draft.verdict !== "pass" && draft.verdict !== "fail") return null;
  const score = draft.score;
  if (score !== null && !(Number.isInteger(score) && score >= 1 && score <= 5)) return null;
  const note = draft.note.trim();
  if (note.length > NOTE_MAX_CHARS) return null;
  return {
    app_name: appName,
    creative_key: creativeKey,
    kind,
    verdict: draft.verdict,
    score,
    note: note || null,
    fail_reasons: draft.verdict === "fail" ? [...new Set(draft.failReasons)] : [],
  };
}

/** The rating the UI shows right after Save, before the server answers. */
export function optimisticRating(payload: RatingPayload, previous?: Rating): Rating {
  return { ...previous, ...payload, updated_at: new Date().toISOString() };
}

/** `{creative_key: rating}`; the newest wins if the list ever holds duplicates. */
export function ratingsByKey(ratings: Rating[]): Record<string, Rating> {
  const out: Record<string, Rating> = {};
  for (const r of ratings) {
    if (!r || typeof r.creative_key !== "string") continue;
    const prev = out[r.creative_key];
    if (!prev || (r.updated_at ?? "") > (prev.updated_at ?? "")) out[r.creative_key] = r;
  }
  return out;
}

/** A proof counts as rated when its visual or its ad copy has a rating. */
export function isProofRated(proof: Proof, byKey: Record<string, Rating>): boolean {
  const keys = creativeKeysFor(proof);
  return Boolean(byKey[keys.visual] || (keys.adCopy && byKey[keys.adCopy]));
}

// ── Judge calibration (GET /ratings/{user}/calibration; runserver/calibration.py) ──

export interface KappaStats {
  n: number;
  agreement: number | null;
  kappa: number | null;
  reason: string | null;
}

export interface CalibrationBlock {
  n: number;
  judge_passed: KappaStats;
  judge_gates_passed: KappaStats;
  score_spearman: { n: number; rho: number | null; reason: string | null };
}

/**
 * Counts and blocks cover only ratings judged by the current judge version
 * (`judge_version`); older APIs omit the versioning fields.
 */
export interface Calibration {
  n: number;
  sessions: number;
  ready_min_ratings: number;
  overall: CalibrationBlock;
  by_kind: Record<RatingKind, CalibrationBlock>;
  /** The current creative_eval JUDGE_VERSION the blocks were computed for. */
  judge_version?: string | null;
  /** Paired ratings from other (earlier or unversioned) judges, not counted. */
  excluded_other_versions?: number;
  /** The same blocks split by whether opt-in rating learning steered the run. */
  by_learning?: { learned: CalibrationBlock; not_learned: CalibrationBlock };
}

/** Default when an older API omits `ready_min_ratings`. */
export const READY_MIN_RATINGS = 20;

const KAPPA_REASONS: Record<string, string> = {
  single_class: "every verdict so far is the same",
  judge_single_class: "the judge gave every creative the same verdict",
  human_single_class: "you gave every creative the same verdict",
};

/**
 * The one-line "Judge agreement" summary: kappa + raw agreement once there are
 * enough ratings paired with a judge verdict, else how many more to rate.
 * Null when the report is malformed.
 */
export function judgeAgreementText(calibration: Calibration | null | undefined): string | null {
  const stats = calibration?.overall?.judge_passed;
  if (!stats || typeof stats.n !== "number") return null;
  const min = calibration?.ready_min_ratings || READY_MIN_RATINGS;
  const excluded = calibration?.excluded_other_versions ?? 0;
  const note =
    excluded > 0
      ? ` (current judge only; ${excluded} earlier ${excluded === 1 ? "rating" : "ratings"} not counted)`
      : "";
  if (stats.n < min) {
    const more = min - stats.n;
    return `Rate ${more} more ${more === 1 ? "creative" : "creatives"} to calibrate the judge${note}`;
  }
  const pct = stats.agreement === null ? "n/a" : `${Math.round(stats.agreement * 100)}%`;
  const kappa =
    stats.kappa === null
      ? `kappa not defined: ${KAPPA_REASONS[stats.reason ?? ""] ?? "too little variation"}`
      : `kappa ${stats.kappa.toFixed(2)}`;
  return `Judge agreement: ${pct} over ${stats.n} ratings, ${kappa}${note}`;
}

/**
 * The second line: judge kappa on runs steered by rating learning vs the rest
 * (current judge only), once both sides have the same minimum of paired
 * ratings as the main line. Null otherwise, or for an older API.
 */
export function learningSplitText(calibration: Calibration | null | undefined): string | null {
  const learned = calibration?.by_learning?.learned?.judge_passed;
  const notLearned = calibration?.by_learning?.not_learned?.judge_passed;
  if (!learned || !notLearned) return null;
  const min = calibration?.ready_min_ratings || READY_MIN_RATINGS;
  if (!(learned.n >= min && notLearned.n >= min)) return null;
  const k = (stats: KappaStats) => (stats.kappa === null ? "n/a" : stats.kappa.toFixed(2));
  return `Learned runs: kappa ${k(learned)} · not learned: kappa ${k(notLearned)}`;
}
