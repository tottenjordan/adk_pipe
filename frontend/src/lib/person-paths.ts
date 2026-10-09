/**
 * Owner-only GCS paths: consented person photos and personalised variants.
 *
 * Run artifacts are shared across users by design, but a person's photo (and any
 * image made from it for one user) is visible only to the user who registered it.
 * Photos live at `person-refs/<slug>/<file>`; personalised variants (PR 3) at
 * `…/variants/<slug>/…`. `<slug>` is `<readable>-<h>`: the owner's email,
 * lower-cased, with `@` and `.` replaced by `_`, plus 10 hex chars of its sha256
 * (mirrors `slug_for` in runserver/person_refs.py).
 *
 * Server-only (node:crypto): imported by the /api/gcs route handler, never by a
 * client component.
 */

import { createHash } from "node:crypto";

export const PERSON_REFS_PREFIX = "person-refs/";

const SLUG_HASH_CHARS = 10;

/** The owner's folder name `<readable>-<h>` (mirrors runserver `slug_for`; shared
 *  golden fixture tests/fixtures/person_slugs.json). `readable` is the normalized
 *  (trimmed, lower-case) email with `@` and `.` replaced by `_`; `h` is the first 10
 *  hex chars of its sha256, which keeps `a.b@x.com` and `a_b@x.com` apart. */
export function emailSlug(email: string): string {
  const normalized = email.trim().toLowerCase();
  const readable = normalized.replace(/[@.]/g, "_");
  const digest = createHash("sha256").update(normalized, "utf8").digest("hex");
  return `${readable}-${digest.slice(0, SLUG_HASH_CHARS)}`;
}

function segments(path: string): string[] {
  return path.replace(/^\/+/, "").split("/");
}

/** True for `person-refs/…` and any path with a `variants` folder segment. */
export function isPersonPath(path: string): boolean {
  const segs = segments(path);
  return segs[0] === "person-refs" || segs.slice(0, -1).includes("variants");
}

/** The owner slug of a person path (`person-refs/<slug>/…` or `…/variants/<slug>/…`),
 *  or null when it has none (such a path is owner-less and never served to a user). */
export function personOwnerSlug(path: string): string | null {
  const segs = segments(path);
  const at = segs[0] === "person-refs" ? 1 : segs.slice(0, -1).indexOf("variants") + 1;
  // The slug must be a non-empty folder (something follows it), never the object itself.
  if (at < 1 || at >= segs.length - 1 || !segs[at]) return null;
  return segs[at];
}

/** The expected upload prefix for a user: `gs://<bucket>/person-refs/<slug>/`. */
export function personRefsPrefix(bucket: string, email: string): string {
  return `gs://${bucket}/${PERSON_REFS_PREFIX}${emailSlug(email)}/`;
}

export type PersonAccess = "allow" | "not_found" | "unauthenticated";

/** Who may read a person path: the owner (slug match), or anyone in local dev.
 *  `who` is `resolveUser`'s result. */
export function personPathAccess(
  path: string,
  who: { kind: "user"; userId: string } | { kind: "local" } | { kind: "reject" },
): PersonAccess {
  if (who.kind === "reject") return "unauthenticated";
  if (who.kind === "local") return "allow";
  const owner = personOwnerSlug(path);
  return owner !== null && owner === emailSlug(who.userId) ? "allow" : "not_found";
}
