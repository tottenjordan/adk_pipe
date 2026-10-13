/**
 * Helpers for the same-origin GCS proxy (`src/app/api/gcs/route.ts`).
 *
 * The proxy serves a bucket object at
 *   /api/gcs?bucket=<bucket>&path=<object-path>
 * These builders keep that URL construction in one place so the query-param
 * names and encoding stay consistent across every call site.
 */

/**
 * A GCS bucket name: lowercase letters, digits, `.`, `_`, `-`, 3-222 chars, starting
 * and ending with a letter or digit. Anything URL-like (`/`, `?`, `#`, `%`) fails, which
 * keeps a bucket from rewriting the JSON API URL it is spliced into.
 */
export const BUCKET_RE = /^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$/;

/** Build the same-origin GCS proxy URL for a bucket + object path. */
export function gcsProxyUrl(bucket: string, path: string): string {
  return `/api/gcs?bucket=${encodeURIComponent(bucket)}&path=${encodeURIComponent(path)}`;
}

/**
 * Parse a `gs://bucket/object/path` URI into its bucket + path parts, or return
 * null if the input is not a `gs://` URI, has no bucket or has no object path.
 */
export function parseGsUri(
  uri: unknown
): { bucket: string; path: string } | null {
  if (typeof uri !== "string" || !uri.startsWith("gs://")) return null;
  const withoutPrefix = uri.replace(/^gs:\/\//, "");
  const slashIdx = withoutPrefix.indexOf("/");
  if (slashIdx < 0) return null;
  const bucket = withoutPrefix.slice(0, slashIdx);
  const path = withoutPrefix.slice(slashIdx + 1);
  // `gs://bucket/` (no object) and `gs:///obj` (no bucket) are not object URIs.
  if (!bucket || !path) return null;
  return { bucket, path };
}
