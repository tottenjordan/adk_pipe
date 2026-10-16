import { isCreativeAgent } from "@/lib/agents";
import type { CampaignInput } from "@/lib/types";

/** The New-run form's required fields, in DOM order (see lib/form-validation). */
export type RequiredField =
  | "brand"
  | "targetProduct"
  | "targetAudience"
  | "keySellingPoints"
  | "targetSearchTrend";

/** Each required field's input id. */
export const FIELD_IDS: Record<RequiredField, string> = {
  brand: "brand",
  targetProduct: "product",
  targetAudience: "audience",
  keySellingPoints: "selling-points",
  targetSearchTrend: "trend",
};

const MESSAGES: Record<RequiredField, string> = {
  brand: "Enter the brand name.",
  targetProduct: "Enter the target product.",
  targetAudience: "Describe the target audience.",
  keySellingPoints: "Add the key selling points.",
  targetSearchTrend: "Enter the search trend to build on.",
};

/** The required fields the form still lacks (blank or whitespace), in DOM order. */
export function missingRequired(form: CampaignInput): RequiredField[] {
  const fields: RequiredField[] = ["brand", "targetProduct", "targetAudience", "keySellingPoints"];
  if (isCreativeAgent(form.agent)) fields.push("targetSearchTrend");
  return fields.filter((f) => !form[f]?.trim());
}

/** A message per required field to flag (missing and shown). */
export type FieldErrors = Partial<Record<RequiredField, string>>;

/** Messages for the missing fields among `shown` (left by the user, or all after a blocked submit). */
export function fieldErrors(form: CampaignInput, shown: ReadonlySet<RequiredField>): FieldErrors {
  return Object.fromEntries(
    missingRequired(form)
      .filter((f) => shown.has(f))
      .map((f) => [f, MESSAGES[f]])
  );
}

/** aria-required, plus aria-invalid and the message's id while it has an error. */
export function requiredProps(field: RequiredField, error: string | undefined) {
  return {
    "aria-required": true,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": error ? `${FIELD_IDS[field]}-error` : undefined,
  } as const;
}

/** The field's inline message (described-by target), or nothing. */
export function FieldError({ field, error }: { field: RequiredField; error: string | undefined }) {
  if (!error) return null;
  return (
    <p id={`${FIELD_IDS[field]}-error`} className="text-xs text-mark-fail">
      {error}
    </p>
  );
}
