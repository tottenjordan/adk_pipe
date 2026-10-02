import type { CampaignInput } from "@/lib/types";

type ValidatedFields = Pick<
  CampaignInput,
  | "agent"
  | "brand"
  | "targetAudience"
  | "targetProduct"
  | "keySellingPoints"
  | "targetSearchTrend"
>;

/**
 * True when the home form has everything the selected agent needs: the four
 * campaign fields always, plus a target search trend for both creative agents.
 * Whitespace-only values count as empty.
 */
export function isFormValid(form: ValidatedFields): boolean {
  const filled = (v: string | undefined) => Boolean(v?.trim());
  const base =
    filled(form.brand) &&
    filled(form.targetAudience) &&
    filled(form.targetProduct) &&
    filled(form.keySellingPoints);
  if (!base) return false;
  if (form.agent === "trend_scout") return true;
  return filled(form.targetSearchTrend);
}
