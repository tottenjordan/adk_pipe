/** Assign a color to each pipeline widget by keyword. */
export function widgetAccent(label: string): { dot: string; badge: string; text: string } {
  if (label.toLowerCase().includes("visual") && label.toLowerCase().includes("final"))
    return { dot: "bg-violet-500", badge: "bg-violet-50 text-violet-600 border-violet-200", text: "text-violet-600" };
  if (label.toLowerCase().includes("critique"))
    return { dot: "bg-orange-500", badge: "bg-orange-50 text-orange-600 border-orange-200", text: "text-orange-600" };
  if (label.toLowerCase().includes("visual"))
    return { dot: "bg-cyan-500", badge: "bg-cyan-50 text-cyan-600 border-cyan-200", text: "text-cyan-600" };
  if (label.toLowerCase().includes("ad copy"))
    return { dot: "bg-indigo-500", badge: "bg-indigo-50 text-indigo-600 border-indigo-200", text: "text-indigo-600" };
  return { dot: "bg-emerald-500", badge: "bg-emerald-50 text-emerald-600 border-emerald-200", text: "text-emerald-600" };
}

/** Extract the list of items from pipeline state data (dict with one key holding an array). */
export function extractItems(data: unknown): Record<string, unknown>[] | null {
  if (!data || typeof data !== "object") return null;
  const obj = data as Record<string, unknown>;
  const keys = Object.keys(obj);
  for (const k of keys) {
    if (Array.isArray(obj[k])) return obj[k] as Record<string, unknown>[];
  }
  return null;
}

/** A per-concept draft edited in the checkpoint-3 review panel. */
export interface ConceptDraft {
  image_generation_prompt: string;
  aspect_ratio: string;
  visual_style: string;
  revision_note: string;
}

/** One entry in the resume `edits` payload (only changed fields + notes). */
export interface ConceptEdit {
  index: number;
  image_generation_prompt?: string;
  aspect_ratio?: string;
  visual_style?: string;
  revision_note?: string;
}

/**
 * Diff the user's edited concept drafts against the originals from session
 * state, producing the minimal `edits` array the resume endpoint expects.
 *
 * Only concepts with a changed direct field (image_generation_prompt /
 * aspect_ratio / visual_style) or a non-empty revision note are included; each
 * entry carries its 0-based `index` and only the fields that actually changed
 * (notes are trimmed). Pure — no dependence on the number/order beyond index.
 */
export function buildConceptEdits(
  originals: Record<string, unknown>[],
  drafts: ConceptDraft[]
): ConceptEdit[] {
  const edits: ConceptEdit[] = [];
  const directFields = [
    "image_generation_prompt",
    "aspect_ratio",
    "visual_style",
  ] as const;

  drafts.forEach((draft, index) => {
    const original = originals[index] ?? {};
    const edit: ConceptEdit = { index };
    let changed = false;

    for (const field of directFields) {
      const before = String(original[field] ?? "");
      const after = draft[field] ?? "";
      if (after !== before) {
        edit[field] = after;
        changed = true;
      }
    }

    const note = (draft.revision_note ?? "").trim();
    if (note) {
      edit.revision_note = note;
      changed = true;
    }

    if (changed) edits.push(edit);
  });

  return edits;
}

/**
 * Normalize the candidate trend terms from session state `raw_gtrends`.
 * The backend stores a `string[]` of ~25 terms; guard against absent/malformed
 * values and drop empties so the panel renders cleanly.
 */
export function parseRawGtrends(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((t): t is string => typeof t === "string")
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
}
