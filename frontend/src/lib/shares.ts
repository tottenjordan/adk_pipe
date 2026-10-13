/** Share links (runserver/shares.py): a frozen, revocable public snapshot of a run's
 *  slate or a single creative. Types, friendly error copy and small pure helpers. */

import { gateLabel } from "@/lib/eval-dimensions";
import { proofFailedChecks, type Proof } from "@/lib/eval-matching";

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
   *  doesn't cover public links (`reason: "person_not_shareable"`) or a timely
   *  creative whose moment has passed (`reason: "timely_expired"`). */
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
  timely_expired:
    "This creative referenced a moment that has passed, so it can't be shared. Share an evergreen creative instead.",
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

/** "N creative(s) showing a person were left out …" and "N timely creative(s) past
 *  their moment were left out." for a slate share's `skipped` list (one sentence
 *  per reason, unknown reasons ignored), or null when nothing known was left out. */
export function skippedNotice(skipped: SkippedCreative[] | undefined): string | null {
  const count = (reason: string) => (skipped ?? []).filter((s) => s.reason === reason).length;
  const person = count("person_not_shareable");
  const expired = count("timely_expired");
  const parts: string[] = [];
  if (person > 0) {
    parts.push(
      person === 1
        ? "1 creative showing a person was left out (their consent doesn't cover public links)."
        : `${person} creatives showing a person were left out (their consent doesn't cover public links).`,
    );
  }
  if (expired > 0) {
    parts.push(
      expired === 1
        ? "1 timely creative past its moment was left out."
        : `${expired} timely creatives past their moment were left out.`,
    );
  }
  return parts.length > 0 ? parts.join(" ") : null;
}

/** A creative about to be shared that failed a blocking judge check (labels deduped). */
export interface FailedCheck {
  conceptName: string;
  labels: string[];
}

/** The blocking failed checks of these proofs, one entry per failing creative
 *  (none for passing creatives or reports written before gates). */
export function failedChecksFor(proofs: readonly Proof[]): FailedCheck[] {
  return proofs.flatMap((p) => {
    const labels = [...new Set(proofFailedChecks(p).map((g) => gateLabel(g.gate)))];
    return labels.length > 0 ? [{ conceptName: p.concept.concept_name, labels }] : [];
  });
}

/** The share dialog's warning (never a block) before a link is made for creatives
 *  that failed a check, or null when none failed. */
export function failedCheckNotice(failed: readonly FailedCheck[], single: boolean): string | null {
  if (failed.length === 0) return null;
  if (single) {
    return `This creative failed a quality check (${failed[0].labels.join(", ")}). Anyone with the link will see it.`;
  }
  const list = failed.map((f) => `${f.conceptName} (${f.labels.join(", ")})`).join("; ");
  const them = failed.length === 1 ? "it" : "them";
  return `${failed.length} of these creatives failed a quality check: ${list}. Anyone with the link will see ${them}.`;
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
