/**
 * Kickoff guard for the async-job run model.
 *
 * The run page's in-memory `startedRef` prevents a double kick-off within one
 * mount, but it resets on every full browser reload — so without a durable
 * guard, reloading `/run/[sessionId]` would call `startRun` again and spawn a
 * SECOND detached run on the same session (and, if the first already finished,
 * re-run the whole pipeline from scratch — duplicate GCS artifacts + BQ rows).
 *
 * We record the claim in `sessionStorage`, which survives reload within the tab
 * but is scoped to it — the same place the run message already lives
 * (`run:${sessionId}`), so a fresh tab (no message) can't kick off anyway. On
 * reload the guard returns true and the page skips `startRun`, going straight to
 * polling (which replays from `since=0`).
 */

const key = (sessionId: string) => `run:${sessionId}:started`;

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
    // sessionStorage unavailable — the in-memory startedRef still guards this mount.
  }
}
