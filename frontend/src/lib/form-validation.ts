import { invalidReferenceUris } from "@/lib/reference-images";
import type { CampaignInput } from "@/lib/types";

type ValidatedFields = Pick<
  CampaignInput,
  | "agent"
  | "brand"
  | "targetAudience"
  | "targetProduct"
  | "keySellingPoints"
  | "targetSearchTrend"
  | "referenceImageUri"
  | "referenceImageRole"
  | "extraReferenceImages"
>;

/**
 * True when the home form has everything the selected agent needs: the four
 * campaign fields always, plus a target search trend for both creative agents.
 * Whitespace-only values count as empty. For the creative agents (the only
 * ones that show reference rows) every non-blank reference row must be a
 * gs:// or http(s) URI.
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
  return filled(form.targetSearchTrend) && invalidReferenceUris(form).length === 0;
}
