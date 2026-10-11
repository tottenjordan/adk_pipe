/** Personalised variant previews (runserver/variants.py): one finished concept
 *  re-rendered with one of the caller's consented people (for a creative that
 *  already casts a person, a "person swap" with someone else). UI only: never
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

/** `GET` `castable[concept]`: whether `POST` would accept this creative now (the
 *  api's own checks), with a plain-language `reason` when it wouldn't. `code` is
 *  `not_ready` | `style_not_person_safe` | `no_human_subject` | `not_castable` |
 *  `not_found` (`""` when ok); `style` is the creative's style family. `mode` is
 *  `"swap"` when the creative already casts a person (`cast_consent_id`, the
 *  caller's own consent, can't be swapped in again) and `"add"` otherwise; both
 *  are absent from an older api (then treat it as `"add"`). */
export interface Castability {
  ok: boolean;
  reason: string;
  code: string;
  style: string;
  mode?: "add" | "swap";
  cast_consent_id?: string;
}

/** The parsed `GET` body. `castable` is null when the api doesn't report it (then
 *  the panel keeps Render enabled and the api explains a refusal after the click). */
export interface VariantsListing {
  variants: VariantMap;
  castable: Record<string, Castability> | null;
  personSafeStyles: string[];
}

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

/** Shown (with a People link) when a swap has no one else to swap in. */
export const NO_OTHER_PERSON_NOTE = "Register another person on the People page to swap.";

/**
 * Whether the proof dialog offers "Personalise" (or "Swap person" for a creative
 * that already casts someone): the creative has a rendered base image. A concept
 * whose style or subject can't feature a person is refused with its reason.
 */
export function canPersonalise(
  proof: { concept: { concept_name: string }; casting?: { cast: boolean } },
  generatedImages: unknown,
  imagesMissing: boolean,
): boolean {
  if (imagesMissing) return false;
  return hasRenderedImage(generatedImages, proof.concept.concept_name);
}

/** The consent a swap must leave out of the picker (`""` when not a swap). */
export function castConsentToSkip(entry: Castability | undefined): string {
  return entry?.mode === "swap" ? (entry.cast_consent_id ?? "") : "";
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

/** A clean castability entry, or null when malformed (optional fields dropped
 *  unless well-typed). */
function toCastability(value: unknown): Castability | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (
    typeof v.ok !== "boolean" ||
    typeof v.reason !== "string" ||
    typeof v.code !== "string" ||
    typeof v.style !== "string"
  ) {
    return null;
  }
  const out: Castability = { ok: v.ok, reason: v.reason, code: v.code, style: v.style };
  if (v.mode === "add" || v.mode === "swap") out.mode = v.mode;
  if (typeof v.cast_consent_id === "string") out.cast_consent_id = v.cast_consent_id;
  return out;
}

/** The whole GET body: previews, per-concept castability and the person-safe styles. */
export function parseVariantsListing(raw: unknown): VariantsListing {
  const body = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  let castable: Record<string, Castability> | null = null;
  if (body.castable && typeof body.castable === "object") {
    castable = {};
    for (const [concept, entry] of Object.entries(body.castable as Record<string, unknown>)) {
      const parsed = toCastability(entry);
      if (parsed) castable[concept] = parsed;
    }
  }
  const styles = Array.isArray(body.person_safe_styles) ? body.person_safe_styles : [];
  return {
    variants: parseVariants(raw),
    castable,
    personSafeStyles: styles.filter((s): s is string => typeof s === "string"),
  };
}

/**
 * Why Render is disabled for this creative, or null when it can be rendered (or
 * castability is unknown). A style refusal names the person-safe styles and the
 * creative's style; any other refusal shows the api's reason as given.
 */
export function castabilityNote(entry: Castability | undefined, safeStyles: string[]): string | null {
  if (!entry || entry.ok) return null;
  if (entry.code === "style_not_person_safe" && safeStyles.length > 0) {
    const tail = entry.style ? `This creative is ${entry.style}.` : "This creative's style isn't one of them.";
    return `Personalised previews work on person-safe styles (${safeStyles.join(", ")}). ${tail}`;
  }
  return entry.reason || "This creative can't feature a person.";
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
  same_person_as_cast: "This creative already features this person.",
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
