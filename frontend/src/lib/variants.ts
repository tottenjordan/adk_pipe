/** Personalised variant previews (runserver/variants.py): one finished, uncast
 *  concept re-rendered with one of the caller's consented people. UI only: never
 *  judged, rated, shared or used in experiments. Types, error copy and pure helpers
 *  for the results page's Personalise panel. */

import { imageCheckFor, type ImageCheck } from "./eval-matching";
import { gcsProxyUrl, parseGsUri } from "./gcs";
import { hasRenderedImage } from "./shares";

export type VariantStatus = "queued" | "rendering" | "done" | "failed" | "rejected";

/** One `person_variants[concept][key]` record. */
export interface VariantRecord {
  status: VariantStatus;
  consent_id?: string;
  created_at?: string;
  gcs_uri?: string | null;
  qa?: { passed?: boolean; failures?: string[] } | null;
  attempts?: number;
  reason?: string | null;
}

/** `POST /variants/{u}/{app}/{session}`: the record plus its key. */
export interface VariantResponse extends VariantRecord {
  key: string;
  concept_name: string;
  cached: boolean;
}

/** `GET /variants/{u}/{app}/{session}`: `{concept: {key: record}}`. */
export type VariantMap = Record<string, Record<string, VariantRecord>>;

/** How often the panel re-reads a queued / rendering preview. */
export const VARIANT_POLL_MS = 5000;

/** Consecutive failed polls (an error or a missing record) before the panel stops. */
export const MAX_POLL_MISSES = 6;

/** Shown (with Retry) once polling gave up. */
export const POLL_STOPPED_MESSAGE =
  "We stopped checking on this preview because its status couldn't be read. Retry to check again.";

/** The muted note under every preview. */
export const VARIANT_NOTE = "Personalised preview — not used in experiments or share links";

const STATUSES: readonly VariantStatus[] = ["queued", "rendering", "done", "failed", "rejected"];

export function isVariantStatus(value: unknown): value is VariantStatus {
  return typeof value === "string" && (STATUSES as readonly string[]).includes(value);
}

/**
 * Whether the proof dialog offers "Personalise": the creative has a rendered base
 * image and doesn't already cast a person (the api refuses `concept_already_cast`;
 * a concept whose style or subject can't feature a person is refused with its reason).
 */
export function canPersonalise(
  proof: { concept: { concept_name: string }; casting?: { cast: boolean } },
  generatedImages: unknown,
  imagesMissing: boolean,
): boolean {
  if (imagesMissing || proof.casting?.cast) return false;
  return hasRenderedImage(generatedImages, proof.concept.concept_name);
}

/** Still being made: keep polling. */
export function isPending(status: VariantStatus | undefined): boolean {
  return status === "queued" || status === "rendering";
}

/** The GET body as a clean map (malformed entries dropped). */
export function parseVariants(raw: unknown): VariantMap {
  const out: VariantMap = {};
  const variants = raw && typeof raw === "object" ? (raw as { variants?: unknown }).variants : undefined;
  if (!variants || typeof variants !== "object") return out;
  for (const [concept, byKey] of Object.entries(variants as Record<string, unknown>)) {
    if (!byKey || typeof byKey !== "object") continue;
    const records: Record<string, VariantRecord> = {};
    for (const [key, rec] of Object.entries(byKey as Record<string, unknown>)) {
      if (rec && typeof rec === "object" && isVariantStatus((rec as VariantRecord).status)) {
        records[key] = rec as VariantRecord;
      }
    }
    if (Object.keys(records).length > 0) out[concept] = records;
  }
  return out;
}

/** The newest variant of `concept` made with `consentId` (by `created_at`), if any. */
export function latestVariant(
  variants: VariantMap,
  concept: string,
  consentId: string,
): { key: string; record: VariantRecord } | null {
  let best: { key: string; record: VariantRecord } | null = null;
  for (const [key, record] of Object.entries(variants[concept] ?? {})) {
    if (record.consent_id !== consentId) continue;
    if (!best || (record.created_at ?? "") > (best.record.created_at ?? "")) best = { key, record };
  }
  return best;
}

/** The proxy URL of a finished variant (`/api/gcs` serves `variants/<slug>/` to its owner only). */
export function variantImageUrl(record: VariantRecord | null | undefined): string | null {
  if (!record || record.status !== "done") return null;
  const parsed = parseGsUri(record.gcs_uri);
  return parsed ? gcsProxyUrl(parsed.bucket, parsed.path) : null;
}

/** The variant's image-check result (same shape as a base proof's). */
export function variantImageCheck(record: VariantRecord | null | undefined): ImageCheck | undefined {
  if (!record || record.status !== "done") return undefined;
  return imageCheckFor({ v: record }, "v");
}

/** Plain-language status line for the aria-live region. */
export function variantStatusText(record: VariantRecord | null | undefined): string {
  if (!record) return "";
  switch (record.status) {
    case "queued":
      return "Queued: previews wait their turn behind other image renders.";
    case "rendering":
      return "Rendering the preview…";
    case "done":
      return "Preview ready.";
    case "rejected":
      return "The safety filter declined this person's photo for this creative, so no preview was made.";
    case "failed":
      return record.reason === "photo_unavailable"
        ? "The person's photo couldn't be read, so no preview was made."
        : record.reason === "interrupted"
          ? "The preview was interrupted. Try again."
          : "The preview couldn't be made. Try again.";
  }
}

/** A failed variants call: `reason` is the backend's `detail.reason` (null if absent). */
export class VariantError extends Error {
  readonly reason: string | null;
  readonly status: number;
  constructor(reason: string | null, status: number, message?: string | null) {
    super(variantErrorMessage(reason, status, message));
    this.name = "VariantError";
    this.reason = reason;
    this.status = status;
  }
}

const MESSAGES: Record<string, string> = {
  consent_not_active: "That person isn't registered any more. Pick someone else.",
  concept_already_cast: "This creative already features a person.",
  concept_not_found: "This creative isn't in the run any more.",
  variant_cap_reached: "You've reached today's limit of personalised previews. Try again tomorrow.",
  run_in_progress: "Wait for the run to finish, then try again.",
  invalid_output_folder: "This run has no output folder, so previews can't be saved.",
  variants_unconfigured: "Personalised previews aren't set up on this server yet.",
  variant_failed: "The preview couldn't be queued. Try again.",
};

/** Friendly message for a variants error reason (falls back on the HTTP status). */
export function variantErrorMessage(reason: string | null, status: number, message?: string | null): string {
  // The not-castable reason is specific (style or subject), so show the server's words.
  if (reason === "concept_not_castable") {
    return message ? `This creative ${message}.` : "This creative can't feature a person.";
  }
  if (reason && MESSAGES[reason]) return MESSAGES[reason];
  if (status === 401 || status === 403) return "You don't have access to this. Reload the page and try again.";
  if (status === 404) return "This run isn't available.";
  return `Something went wrong (${status}). Try again.`;
}
