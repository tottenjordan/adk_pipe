/**
 * Kickoff guard for the async-job run model.
 *
 * The run page remounts on every full browser reload, and React StrictMode
 * (`next dev`) mounts it twice, so the kick-off needs two layers of protection
 * against spawning a SECOND detached run on the same session (and, if the first
 * already finished, re-running the whole pipeline — duplicate GCS artifacts +
 * BQ rows):
 *
 * - a durable claim in `sessionStorage` (survives reload within the tab, scoped
 *   to it — the same place the run message lives, `run:${sessionId}`, so a fresh
 *   tab with no message can't kick off anyway). On reload the page skips
 *   `startRun` and goes straight to polling, which replays from `since=0`.
 * - a module-level in-flight map, so two effect runs that race before the first
 *   `startRun` resolves (StrictMode's mount → cleanup → mount) share ONE call.
 */

const key = (sessionId: string) => `run:${sessionId}:started`;
const messageKey = (sessionId: string) => `run:${sessionId}`;

/** In-flight kick-offs, keyed by session id (cleared when each settles). */
const inFlight = new Map<string, Promise<void>>();

/** True once a run has been kicked off for this session in this tab. */
export function hasStartedRun(sessionId: string): boolean {
  try {
    return sessionStorage.getItem(key(sessionId)) === "1";
  } catch {
    // sessionStorage unavailable (SSR / private mode) — best-effort only.
    return false;
  }
}

/** Record that a run has been kicked off for this session. */
export function markRunStarted(sessionId: string): void {
  try {
    sessionStorage.setItem(key(sessionId), "1");
  } catch {
    // sessionStorage unavailable — the in-flight map still guards this page load.
  }
}

/**
 * Kick off a run at most once per session. Resolves immediately when the tab
 * already started it; joins the in-flight call when one is pending; otherwise
 * calls `start` and marks the session started on success. A failed `start` is
 * NOT marked (and leaves the map), so the error surfaces and a later attempt
 * may try again.
 */
export function ensureRunStarted(
  sessionId: string,
  start: () => Promise<unknown>
): Promise<void> {
  if (hasStartedRun(sessionId)) return Promise.resolve();
  const pending = inFlight.get(sessionId);
  if (pending) return pending;
  const claim = (async () => {
    try {
      await start();
      markRunStarted(sessionId);
    } finally {
      inFlight.delete(sessionId);
    }
  })();
  inFlight.set(sessionId, claim);
  return claim;
}

/** The kick-off message the home form stored for this session, or "". */
export function readRunMessage(sessionId: string): string {
  try {
    const stored = sessionStorage.getItem(messageKey(sessionId));
    if (!stored) return "";
    const parsed: unknown = JSON.parse(stored);
    if (parsed && typeof parsed === "object") {
      const message = (parsed as { message?: unknown }).message;
      return typeof message === "string" ? message : "";
    }
    return "";
  } catch {
    return "";
  }
}

/**
 * True when a poll shows nothing was ever run on this session: the server has
 * no session (`not_found`) or an empty event log. A session that exists but was
 * never kicked off polls as `running` with zero events, so status alone can't
 * tell; any event (or a non-zero cursor) means the run is real and viewable.
 */
export function isUnstartedRun(poll: {
  status: string;
  events?: unknown[] | null;
  nextCursor?: number | null;
}): boolean {
  if ((poll.events?.length ?? 0) > 0 || (poll.nextCursor ?? 0) > 0) return false;
  return poll.status === "not_found" || poll.status === "running";
}
