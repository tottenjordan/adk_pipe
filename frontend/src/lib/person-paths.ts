/**
 * Owner-only GCS paths: consented person photos and personalised variants.
 *
 * Run artifacts are shared across users by design, but a person's photo (and any
 * image made from it for one user) is visible only to the user who registered it.
 * Photos live at `person-refs/<slug>/<file>`; personalised variants (PR 3) at
 * `…/variants/<slug>/…`. `<slug>` is the owner's email, lower-cased, with `@` and
 * `.` replaced by `_` (mirrors `slug_for` in runserver/person_refs.py).
 */

export const PERSON_REFS_PREFIX = "person-refs/";

/** `admin@x.com` → `admin_x_com` (mirrors runserver `slug_for`). */
export function emailSlug(email: string): string {
  return email.trim().toLowerCase().replace(/[@.]/g, "_");
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
