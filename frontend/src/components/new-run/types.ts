import type { Dispatch, SetStateAction } from "react";
import type { CampaignInput } from "@/lib/types";

/** The New-run form state and its setter, shared by the form's fieldsets. */
export interface FormSectionProps {
  form: CampaignInput;
  setForm: Dispatch<SetStateAction<CampaignInput>>;
}
