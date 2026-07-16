/**
 * Helpers for the same-origin GCS proxy (`src/app/api/gcs/route.ts`).
 *
 * The proxy serves a bucket object at
 *   /api/gcs?bucket=<bucket>&path=<object-path>
 * These builders keep that URL construction in one place so the query-param
 * names and encoding stay consistent across every call site.
 */

/** Build the same-origin GCS proxy URL for a bucket + object path. */
export function gcsProxyUrl(bucket: string, path: string): string {
  return `/api/gcs?bucket=${encodeURIComponent(bucket)}&path=${encodeURIComponent(path)}`;
}

/**
 * Parse a `gs://bucket/object/path` URI into its bucket + path parts, or return
 * null if the input is not a `gs://` URI or has no object path.
 */
export function parseGsUri(
  uri: unknown
): { bucket: string; path: string } | null {
  if (typeof uri !== "string" || !uri.startsWith("gs://")) return null;
  const withoutPrefix = uri.replace(/^gs:\/\//, "");
  const slashIdx = withoutPrefix.indexOf("/");
  if (slashIdx < 0) return null;
  return {
    bucket: withoutPrefix.slice(0, slashIdx),
    path: withoutPrefix.slice(slashIdx + 1),
  };
}
