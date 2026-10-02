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
