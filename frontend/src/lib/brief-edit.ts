/**
 * Editing the structured creative brief at interactive checkpoint 1.
 *
 * The draft is the parsed `CreativeBrief` (camelCase, see `creative-brief.ts`);
 * `toBriefPayload` turns it back into the snake_case object the backend
 * validates against the Python `CreativeBrief` schema
 * (`runserver.async_runs.merge_brief_edit`; invalid → HTTP 400). The client
 * checks the same hard rules first so the user sees them before submitting.
 */

import type { CreativeAngle, CreativeBrief, FitMode } from "./creative-brief";

export const MIN_ANGLES = 3;
export const MAX_ANGLES = 5;
export const BRIEF_EDIT_FIELD = "creative_brief";

export const FIT_MODE_OPTIONS: readonly { value: FitMode; label: string }[] = [
  { value: "direct", label: "Direct fit" },
  { value: "cultural", label: "Cultural fit" },
  { value: "light_touch", label: "Light touch" },
];

/** Field errors keyed by the brief's snake_case path (e.g. `angles.1.name`). */
export type BriefErrors = Record<string, string>;

/** A checkpoint-1 resume edit carrying the full brief. */
export type BriefEdit = { field: typeof BRIEF_EDIT_FIELD; value: Record<string, unknown> };

/** Client-side validation: the rules the backend schema (and the brief) need. */
export function validateBriefDraft(draft: CreativeBrief): BriefErrors {
  const errors: BriefErrors = {};
  if (!draft.singleMindedProposition.trim()) {
    errors.single_minded_proposition = "Add the single-minded proposition.";
  }
  if (draft.angles.length < MIN_ANGLES) {
    errors.angles = `Keep at least ${MIN_ANGLES} angles.`;
  } else if (draft.angles.length > MAX_ANGLES) {
    errors.angles = `Use at most ${MAX_ANGLES} angles.`;
  }
  draft.angles.forEach((angle, i) => {
    if (!angle.name.trim()) errors[`angles.${i}.name`] = "Give this angle a name.";
  });
  const score = draft.trendBridge.fitScore;
  if (score === null || !Number.isInteger(score) || score < 1 || score > 5) {
    errors["trend_bridge.fit_score"] = "Choose a fit score from 1 to 5.";
  }
  if (!draft.trendBridge.fitMode) {
    errors["trend_bridge.fit_mode"] = "Choose a fit mode.";
  }
  return errors;
}

/** The lowest free angle id from A1–A5 (or `A<n+1>` past five). */
export function nextAngleId(angles: CreativeAngle[]): string {
  const used = new Set(angles.map((a) => a.angleId));
  for (let n = 1; n <= MAX_ANGLES; n++) {
    if (!used.has(`A${n}`)) return `A${n}`;
  }
  return `A${angles.length + 1}`;
}

/** A new, empty angle with the next free id. */
export function emptyAngle(angles: CreativeAngle[]): CreativeAngle {
  return { angleId: nextAngleId(angles), name: "", tension: "", route: "" };
}

const clean = (items: string[]): string[] => items.map((s) => s.trim()).filter(Boolean);

/** The draft as the snake_case `CreativeBrief` object the backend validates. */
export function toBriefPayload(draft: CreativeBrief): Record<string, unknown> {
  const tb = draft.trendBridge;
  return {
    objective: draft.objective.trim(),
    audience: draft.audience.trim(),
    insight: draft.insight.trim(),
    single_minded_proposition: draft.singleMindedProposition.trim(),
    reasons_to_believe: draft.reasonsToBelieve
      .map((r) => ({ claim: r.claim.trim(), source_id: r.sourceId?.trim() || null }))
      .filter((r) => r.claim),
    brand: {
      tone_of_voice: draft.brand.toneOfVoice.trim(),
      distinctive_assets: clean(draft.brand.distinctiveAssets),
      do_not: clean(draft.brand.doNot),
    },
    trend_bridge: {
      fit_score: tb.fitScore,
      fit_mode: tb.fitMode,
      bridge: tb.bridge.trim(),
      motifs: clean(tb.motifs),
      risks: clean(tb.risks),
    },
    mandatories: clean(draft.mandatories),
    avoid: clean(draft.avoid),
    desired_response: draft.desiredResponse.trim(),
    angles: draft.angles.map((a, i) => ({
      angle_id: a.angleId.trim() || `A${i + 1}`,
      name: a.name.trim(),
      tension: a.tension.trim(),
      route: a.route.trim(),
    })),
  };
}

/**
 * The checkpoint-1 `edits` entry for an edited brief, or null when the draft
 * is unchanged (compared after the same cleaning: trimmed text, blank rows
 * dropped) so an untouched form sends nothing.
 */
export function buildBriefEdit(original: CreativeBrief, draft: CreativeBrief): BriefEdit[] | null {
  const value = toBriefPayload(draft);
  if (JSON.stringify(value) === JSON.stringify(toBriefPayload(original))) return null;
  return [{ field: BRIEF_EDIT_FIELD, value }];
}
