/**
 * Server-only GCS helpers for the `/api/gcs` proxy.
 *
 * Kept out of `lib/gcs.ts` because that module is imported by client pages; these
 * read `process.env` and build the authenticated JSON API URL.
 */

import { BUCKET_RE } from "@/lib/gcs";

type Env = Record<string, string | undefined>;

/** The `GCS_ALLOWED_BUCKETS` allowlist (comma-separated, trimmed); empty = not configured. */
export function allowedBuckets(env: Env = process.env): string[] {
  return (env.GCS_ALLOWED_BUCKETS ?? "")
    .split(",")
    .map((b) => b.trim())
    .filter(Boolean);
}

/**
 * True when `bucket` is a well-formed bucket name and, if `GCS_ALLOWED_BUCKETS` is set,
 * one of the listed buckets.
 */
export function isAllowedBucket(bucket: string, env: Env = process.env): boolean {
  if (!BUCKET_RE.test(bucket)) return false;
  const allowed = allowedBuckets(env);
  return allowed.length === 0 || allowed.includes(bucket);
}

/** The JSON API media URL for one object; both segments are encoded. */
export function gcsMediaUrl(bucket: string, object: string): string {
  return `https://storage.googleapis.com/storage/v1/b/${encodeURIComponent(bucket)}/o/${encodeURIComponent(object)}?alt=media`;
}
