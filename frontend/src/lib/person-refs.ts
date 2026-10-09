/** Person references (runserver/person_refs.py): consented photos of people that runs
 *  may cast. Types, friendly error copy and small pure helpers for the /people page. */

export type PersonSubject = "self" | "third_party_with_consent";

/** One consent as returned by POST /person-refs/{u} and GET /person-refs/{u}. */
export interface PersonRef {
  consent_id: string;
  photo_uri: string;
  label: string;
  subject: PersonSubject;
  adult_attested: boolean;
  allow_public_share: boolean;
  consent_text_version: string;
  created_at: string;
}

/** GET /person-refs/{u}: active consents plus the caller's upload prefix
 *  (`gs://<bucket>/person-refs/<slug>/`, null when the api has no bucket). */
export interface PersonRefList {
  personRefs: PersonRef[];
  prefix: string | null;
}

export interface CreatePersonRefPayload {
  photo_uri: string;
  label: string;
  subject: PersonSubject;
  adult_attested: boolean;
  allow_public_share: boolean;
  consent_text_version: string;
}

/** A failed person-refs call: `reason` is the backend's `detail.reason` (null if absent). */
export class PersonRefError extends Error {
  readonly reason: string | null;
  readonly status: number;
  constructor(reason: string | null, status: number) {
    super(personRefErrorMessage(reason, status));
    this.name = "PersonRefError";
    this.reason = reason;
    this.status = status;
  }
}

const MESSAGES: Record<string, string> = {
  invalid_photo_uri: "The photo must be a .jpg, .jpeg, .png or .webp file directly inside your folder shown above.",
  photo_unreadable: "That photo couldn't be read. Check the path, and that it's an image of at most 10 MB.",
  adult_attestation_required: "Confirm the person is an adult who agreed to appear.",
  stale_consent_text: "The consent text has changed. Reload the page and read it again.",
  invalid_subject: "Choose who is in the photo.",
  invalid_label: "Give the person a name of up to 80 characters.",
  invalid_allow_public_share: "Something went wrong with the form. Reload the page and try again.",
  already_registered: "This photo is already registered.",
  too_many_person_refs: "You have too many people registered. Revoke one you no longer need, then try again.",
  person_ref_not_found: "This person is no longer registered.",
  person_refs_unconfigured: "People aren't set up on this server yet.",
  store_failed: "That couldn't be saved. Try again.",
  revoke_incomplete: "Consent is revoked, but not everything was deleted yet. Revoke again to finish.",
};

/** Friendly message for a person-refs error reason (falls back on the HTTP status). */
export function personRefErrorMessage(reason: string | null, status: number): string {
  if (reason && MESSAGES[reason]) return MESSAGES[reason];
  if (status === 401 || status === 403) return "You don't have access to this. Reload the page and try again.";
  if (status === 404) return "People aren't available here.";
  return `Something went wrong (${status}). Try again.`;
}

/** "Me" / "Someone who agreed". */
export function subjectLabel(subject: PersonSubject): string {
  return subject === "self" ? "Me" : "Someone who agreed";
}

/** Shown when the api can't name the caller's folder (no bucket configured); the rule
 *  is `emailSlug` in lib/person-paths.ts / `slug_for` in runserver/person_refs.py. */
export const PREFIX_RULE =
  "gs://<bucket>/person-refs/<your email, lower-cased, with @ and . replaced by _>/";
