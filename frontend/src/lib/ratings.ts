/**
 * Human creative ratings (judge calibration): types and pure helpers for the
 * results-page rating control. The REST client lives in `lib/api.ts`
 * (`putRating` / `getRatings`); the backend is `runserver/ratings.py`.
 */
import type { Proof } from "./eval-matching";

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
}

/** What the control edits before saving (`verdict` empty until chosen). */
export interface RatingDraft {
  verdict: RatingVerdict | "";
  score: number | null;
  note: string;
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

export function draftFrom(rating?: Rating): RatingDraft {
  return {
    verdict: rating?.verdict ?? "",
    score: rating?.score ?? null,
    note: rating?.note ?? "",
  };
}

/** True when the draft would change the saved rating (or there is none yet). */
export function isDirty(draft: RatingDraft, saved?: Rating): boolean {
  const base = draftFrom(saved);
  return (
    draft.verdict !== base.verdict ||
    draft.score !== base.score ||
    draft.note.trim() !== base.note.trim()
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
