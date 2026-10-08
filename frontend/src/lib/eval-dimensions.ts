/** Short human labels for the 12 creative_eval dimensions. */
export const DIMENSION_LABELS: Readonly<Record<string, string>> = {
  // Ad copy
  strategic_alignment: "Strategy fit",
  trend_authenticity: "Trend authenticity",
  platform_viability: "Platform fit",
  copy_quality: "Copy quality",
  audience_fit: "Audience fit",
  call_to_action_strength: "Call to action",
  // Visual concept
  trend_visual_connection: "Trend connection",
  brand_product_representation: "Brand & product",
  audience_appeal: "Audience appeal",
  prompt_technical_quality: "Prompt quality",
  stopping_power: "Stopping power",
  concept_coherence: "Coherence",
};

/** Label for a dimension; unknown snake_case names become sentence case. */
export function dimensionLabel(dimension: string): string {
  if (Object.hasOwn(DIMENSION_LABELS, dimension)) return DIMENSION_LABELS[dimension];
  const words = dimension.replace(/_+/g, " ").trim().replace(/\s+/g, " ").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Short human labels for the creative_eval binary compliance gates. */
export const GATE_LABELS: Readonly<Record<string, string>> = {
  // Ad copy
  delivers_proposition: "Delivers the proposition",
  product_named: "Product named",
  uses_reason_to_believe: "Uses a reason to believe",
  mandatories_met: "Mandatories met",
  // Shared
  avoid_respected: "Avoid list respected",
  trend_risks_respected: "Respects trend risks",
  // Visual concept
  product_visible: "Product visible",
  trend_motif_visible: "Trend motif visible",
  text_correct: "In-image text correct",
  brand_cue_present: "Brand cue present",
  no_visual_defects: "No visual defects",
  // The judge skipped every check (one failed entry; mirrors creative_eval)
  gates_reported: "Checks reported",
};

/** Label for a gate; unknown names fall back like `dimensionLabel`. */
export function gateLabel(gate: string): string {
  if (Object.hasOwn(GATE_LABELS, gate)) return GATE_LABELS[gate];
  return dimensionLabel(gate);
}
