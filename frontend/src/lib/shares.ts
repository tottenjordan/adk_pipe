/** Share links (runserver/shares.py): a frozen, revocable public snapshot of a run's
 *  slate or a single creative. Types, friendly error copy and small pure helpers. */

export type ShareScope = "slate" | "creative";

/** One share as returned by POST /shares/{u}/{app}/{session} and GET /shares/{u}. */
export interface Share {
  token: string;
  /** Absolute when the api has SHARE_BASE_URL, else relative `/s/<token>`. */
  url: string;
  title: string;
  scope: ShareScope;
  include_eval: boolean;
  concept_names: string[];
  app_name: string;
  session_id: string;
  created_at: string;
  /** POST only: creatives a slate share left out, e.g. a person whose consent
   *  doesn't cover public links (`reason: "person_not_shareable"`). */
  skipped?: SkippedCreative[];
}

export interface SkippedCreative {
  concept_name: string;
  reason: string;
}

export interface CreateSharePayload {
  /** 1–4 concept names for a single-creative share; omit to share the whole slate. */
  concept_names?: string[];
  include_eval?: boolean;
}

/** A failed shares call: `reason` is the backend's `detail.reason` (null if absent). */
export class ShareError extends Error {
  readonly reason: string | null;
  readonly status: number;
  constructor(reason: string | null, status: number) {
    super(shareErrorMessage(reason, status));
    this.name = "ShareError";
    this.reason = reason;
    this.status = status;
  }
}

const MESSAGES: Record<string, string> = {
  invalid_app_name: "This run can't be shared.",
  invalid_concept_names: "That selection can't be shared. Reload the page and try again.",
  invalid_include_eval: "That selection can't be shared. Reload the page and try again.",
  unknown_concept: "That creative is no longer in this run. Reload the page and try again.",
  no_images: "There are no rendered images to share yet.",
  person_not_shareable:
    "This creative shows a person whose consent doesn't cover public links, so it can't be shared.",
  consent_unavailable: "Couldn't check the consent of the people shown. Try again shortly.",
  person_consent_changed:
    "A person's consent changed while the link was being created, so it wasn't shared. Try again.",
  image_outside_bucket: "These images aren't stored in this run's output folder, so they can't be shared.",
  session_not_found: "This run could not be found.",
  share_not_found: "This link no longer exists.",
  too_many_shares: "You have too many active links. Revoke one you no longer need, then try again.",
  share_failed: "The link could not be created. Try again.",
  store_failed: "The link could not be saved. Try again.",
  revoke_incomplete: "The link is revoked, but its images weren't fully removed. Revoke again to finish.",
  shares_unconfigured: "Sharing isn't set up on this server yet.",
};

/** Friendly message for a shares error reason (falls back on the HTTP status). */
export function shareErrorMessage(reason: string | null, status: number): string {
  if (reason && MESSAGES[reason]) return MESSAGES[reason];
  if (status === 401 || status === 403) return "You don't have access to this. Reload the page and try again.";
  if (status === 404) return "Sharing isn't available here.";
  return `Something went wrong (${status}). Try again.`;
}

/** "N creative(s) showing a person were left out …" for a slate share's `skipped`
 *  list, or null when no creative with a person was left out. */
export function skippedNotice(skipped: SkippedCreative[] | undefined): string | null {
  const n = (skipped ?? []).filter((s) => s.reason === "person_not_shareable").length;
  if (n === 0) return null;
  return n === 1
    ? "1 creative showing a person was left out (their consent doesn't cover public links)."
    : `${n} creatives showing a person were left out (their consent doesn't cover public links).`;
}

/** True for an absolute http(s) URL; a relative `/s/<token>` means SHARE_BASE_URL is unset. */
export function isAbsoluteUrl(url: string): boolean {
  return /^https?:\/\//i.test(url);
}

/** The shares that belong to one run (the list endpoint returns all of the caller's). */
export function sharesForSession(shares: Share[], sessionId: string): Share[] {
  return shares.filter((s) => s.session_id === sessionId);
}

/** True when `generated_images[conceptName]` has a rendered image (`gcs_uri`), i.e. the
 *  creative can be shared (the backend copies exactly that image into the snapshot). */
export function hasRenderedImage(generatedImages: unknown, conceptName: string): boolean {
  if (!generatedImages || typeof generatedImages !== "object") return false;
  const entry = (generatedImages as Record<string, unknown>)[conceptName];
  if (!entry || typeof entry !== "object") return false;
  const uri = (entry as { gcs_uri?: unknown }).gcs_uri;
  return typeof uri === "string" && uri.length > 0;
}
