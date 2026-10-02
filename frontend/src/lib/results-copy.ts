import type { EvalReport } from "@/lib/eval-matching";

/** "3 of 4 ad copies and 2 of 4 visuals pass." */
export function summarySentence(summary: EvalReport["summary"]): string {
  const ads = `${summary.ad_copies_passed} of ${summary.total_ad_copies} ad ${
    summary.total_ad_copies === 1 ? "copy" : "copies"
  }`;
  if (summary.total_visual_concepts === 0) return `${ads} pass.`;
  const visuals = `${summary.visual_concepts_passed} of ${summary.total_visual_concepts} ${
    summary.total_visual_concepts === 1 ? "visual" : "visuals"
  }`;
  return `${ads} and ${visuals} pass.`;
}

/**
 * One-line campaign summary from display fields (see `buildDisplayFields`):
 * "Brand, product, on the X trend". Missing parts are dropped.
 */
export function campaignSummary(fields: { key: string; value: string }[]): string {
  const get = (k: string) => fields.find((f) => f.key === k)?.value.trim() ?? "";
  const lead = [get("brand"), get("target_product")].filter(Boolean).join(", ");
  const trend = get("target_search_trends");
  if (lead && trend) return `${lead}, on the ${trend} trend`;
  return lead || (trend ? `${trend} trend` : "");
}
