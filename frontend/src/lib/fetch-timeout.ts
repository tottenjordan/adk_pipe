/** Upper bound on one Cloud Storage request (headers and body) from a route handler. */
export const GCS_FETCH_TIMEOUT_MS = 15_000;

/**
 * Upper bound on one proxied api request. Generous: polls answer in well under a
 * second and run kick-off / resume return once the detached task is started, but a
 * share create re-encodes up to four images and experiment / metrics calls read
 * BigQuery.
 */
export const BACKEND_FETCH_TIMEOUT_MS = 60_000;

/** True for the rejection of a fetch aborted by `AbortSignal.timeout(...)`. */
export function isTimeoutError(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { name?: unknown }).name === "TimeoutError"
  );
}
