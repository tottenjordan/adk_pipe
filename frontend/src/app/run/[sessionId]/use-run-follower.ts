"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { CONTINUE_MESSAGE } from "@/lib/run-completion";
import {
  startRun,
  pollRun,
  getRunStatus,
  resumeRun,
  ResumeNotAppliedError,
  ResumeRejectedError,
  RunConnectionLostError,
  getSession,
} from "@/lib/api";
import { addStepError, stepErrorMessage } from "@/lib/step-errors";
import { ensureRunStarted, isUnstartedRun, readRunMessage } from "@/lib/run-kickoff";
import type { AgentEvent } from "@/lib/types";
import {
  PendingLongRunningCalls,
  isPauseAnswered,
  type PauseContext,
} from "@/lib/pause-detection";
import { stallTimeoutFor, PAUSE_WATCH_INTERVAL_MS, RUNSERVER_MARKER_AUTHOR } from "./run-config";
import { clearDraftsFor } from "./review-drafts";

/** "connecting": no poll result yet (the kick-off and the seed poll are in flight). */
export type RunStatus =
  | "connecting"
  | "running"
  | "completed"
  | "error"
  | "paused"
  | "stalled"
  | "disconnected";

export interface RunFollower {
  events: AgentEvent[];
  status: RunStatus;
  /** A failed run's message (shown as an alert). */
  errorMsg: string | null;
  /** Non-error, informational notice (e.g. a resume that must be re-submitted). */
  notice: string | null;
  /** Non-fatal step errors (contained by a fail-soft step); see lib/step-errors. */
  stepErrors: string[];
  /** Field errors from a rejected (400) checkpoint resume, shown in the review. */
  reviewErrors: Record<string, string>;
  sessionState: Record<string, unknown>;
  pauseContext: PauseContext | null;
  /** Viewing a session the server has no run for (and no kick-off message). */
  notStarted: boolean;
  /** "Continue run" for a run that stopped early: in flight, and its kick-off error. */
  continuing: boolean;
  continueError: string | null;
  /** Answer the paused checkpoint and follow the resumed run. */
  resume: (response: Record<string, unknown>) => Promise<void>;
  /** Continue a run that stopped early and follow it. */
  continueRun: () => Promise<void>;
}

/** What a run of consecutive poll events changes, applied in one go. */
export interface PollBatch {
  events: AgentEvent[];
  /** The events' state deltas merged in order, or null when none had one. */
  stateDelta: Record<string, unknown> | null;
  /** Step-error messages (lib/step-errors), in order. */
  stepErrors: string[];
}

/**
 * Collects consumed poll events and applies them as one batch per poll page
 * (on the next macrotask: a page's events are yielded back to back, with no
 * timer in between), so a replay of n events costs one O(n) state update
 * instead of n growing array copies. `flush()` applies what is pending at once
 * (a segment end, before the status is decided).
 */
export function createPollBatcher(apply: (batch: PollBatch) => void) {
  let batch: PollBatch = { events: [], stateDelta: null, stepErrors: [] };
  let timer: ReturnType<typeof setTimeout> | null = null;

  function flush() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (batch.events.length === 0) return;
    const ready = batch;
    batch = { events: [], stateDelta: null, stepErrors: [] };
    apply(ready);
  }

  function add(event: AgentEvent) {
    batch.events.push(event);
    const delta = event.actions?.stateDelta;
    if (delta) batch.stateDelta = { ...batch.stateDelta, ...delta };
    const stepErr = stepErrorMessage(event);
    if (stepErr) batch.stepErrors.push(stepErr);
    if (timer === null) timer = setTimeout(flush, 0);
  }

  return { add, flush };
}

/**
 * Follows one detached run: kicks it off once (run-kickoff), polls its event
 * log (deduped across replays from since=0), detects review pauses, flags a
 * quiet run as stalled, watches a paused run for an answer from another tab,
 * and resumes / continues it.
 */
export function useRunFollower(
  appName: string,
  userId: string,
  sessionId: string,
  opts?: { enabled?: boolean }
): RunFollower {
  const enabled = opts?.enabled ?? true;
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [status, setStatus] = useState<RunStatus>("connecting");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [stepErrors, setStepErrors] = useState<string[]>([]);
  const [reviewErrors, setReviewErrors] = useState<Record<string, string>>({});
  const [sessionState, setSessionState] = useState<Record<string, unknown>>({});
  const [pauseContext, setPauseContext] = useState<PauseContext | null>(null);
  const [notStarted, setNotStarted] = useState(false);
  const seenEventIds = useRef(new Set<string>());
  // Seeded by the effects below on mount (no impure Date.now() during render).
  const lastEventAt = useRef<number>(0);
  // Aborts the resume poll loop on unmount (resume is a click handler,
  // not an effect, so it can't own an effect-scoped AbortController itself).
  const resumeAbortRef = useRef<AbortController | null>(null);
  const [continuing, setContinuing] = useState(false);
  const [continueError, setContinueError] = useState<string | null>(null);

  // Fetch full session state so campaign metadata is available. Stable across
  // renders (memoized on the run identity) so it can be a poll-effect dependency
  // without re-arming the effect every render.
  const syncSessionState = useCallback(async () => {
    try {
      const session = await getSession(appName, userId, sessionId);
      if (session.state) {
        setSessionState((prev) => ({ ...prev, ...session.state }));
      }
    } catch {
      // Ignore — session may not exist yet
    }
  }, [appName, userId, sessionId]);

  // Shared poll consumer used by BOTH the initial run effect and the resume
  // path (event-id dedup, error surfacing, setEvents, state-delta merge, and
  // long-running-tool pause detection). The only per-call difference is
  // captured by `opts`:
  //   - syncOnPause: fetch full session state on pause (the initial run does;
  //     the resume path relies on state already loaded).
  // The pause is decided when the poll segment ENDS (a paused segment ends
  // right after its checkpoint call, with a terminal marker): it is the
  // long-running call still unanswered. A long-running call alone is not a
  // pause — pipeline NodeTools are long-running too but are answered in the
  // same segment (see PendingLongRunningCalls).
  // Returns true when a terminal UI state (paused) was already set, so
  // the caller must not override it; false means the run completed normally.
  const consumePollEvents = useCallback(
    async (
      poll: AsyncGenerator<AgentEvent>,
      opts: { syncOnPause?: boolean } = {}
    ): Promise<boolean> => {
      const pending = new PendingLongRunningCalls();
      const batcher = createPollBatcher((batch) => {
        // Activity again: a quiet stretch (e.g. sequential renders) is over.
        setStatus((s) => (s === "stalled" ? "running" : s));

        // An error event (e.g. a model 400/429) is NOT a run failure: a
        // fail-soft step may have contained it and the run carries on. Show it
        // as a non-blocking notice; only the poll status (pollRun throws on
        // "error") fails the run.
        if (batch.stepErrors.length > 0) {
          setStepErrors((prev) => batch.stepErrors.reduce(addStepError, prev));
        }

        setEvents((prev) => prev.concat(batch.events));

        const { stateDelta } = batch;
        if (stateDelta) setSessionState((prev) => ({ ...prev, ...stateDelta }));
      });
      try {
        for await (const event of poll) {
          // Skip the server's internal run-status marker events — status/error
          // come from the poll payload, not these (see RUNSERVER_MARKER_AUTHOR).
          if (event.author === RUNSERVER_MARKER_AUTHOR) continue;

          // Deduplicate events by ID
          if (event.id && seenEventIds.current.has(event.id)) continue;
          if (event.id) seenEventIds.current.add(event.id);

          batcher.add(event);
          // Pause detection still sees every event, in order.
          pending.observe(event);
        }
      } finally {
        // Events consumed before the segment ended (or failed) land first.
        batcher.flush();
      }
      const pause = pending.pause();
      if (!pause) return false;
      setPauseContext(pause);
      setStatus("paused");
      // Fetch full session state so campaign metadata is available
      if (opts.syncOnPause) syncSessionState();
      return true;
    },
    [syncSessionState]
  );

  // A poll or resume failure: a lost connection (the run may still be going on
  // the server) offers a reload; anything else fails the run.
  const failRun = useCallback((err: unknown, fallback: string) => {
    if (err instanceof RunConnectionLostError) {
      setStatus("disconnected");
      return;
    }
    setStatus("error");
    setErrorMsg(err instanceof Error ? err.message : fallback);
  }, []);

  // Follow a run that was just resumed (by this tab or elsewhere) until its next
  // checkpoint or the end. Re-enters pollRun from since=0; seenEventIds dedup
  // makes the replay idempotent. The poll is owned by resumeAbortRef (aborted on
  // unmount), not by an effect, so a status change can't cancel it.
  const followRun = useCallback(async () => {
    resumeAbortRef.current?.abort();
    const controller = new AbortController();
    resumeAbortRef.current = controller;
    try {
      // The resumed segment ends at the next checkpoint (paused) or at the end
      // of the run; it does not re-sync session state on pause.
      const terminal = await consumePollEvents(
        pollRun(appName, userId, sessionId, { signal: controller.signal })
      );
      if (!terminal) setStatus("completed");
    } catch (err) {
      // Unmount aborts the poll — that is not a real resume failure.
      if (
        controller.signal.aborted ||
        (err instanceof Error && err.name === "AbortError")
      ) {
        return;
      }
      failRun(err, "Resume failed");
    }
  }, [appName, userId, sessionId, consumePollEvents, failRun]);

  useEffect(() => {
    if (!enabled) return;
    // Each effect run owns its own poll (and AbortController); cleanup aborts
    // it. Under StrictMode the effect runs twice: the first poll is aborted, the
    // second re-polls from since=0 and the seenEventIds dedup keeps that replay
    // idempotent. The kick-off itself is exactly-once via ensureRunStarted
    // (durable sessionStorage claim + shared in-flight promise).
    const controller = new AbortController();
    const { signal } = controller;
    const message = readRunMessage(sessionId);

    async function run() {
      try {
        // Kick off the detached background run — ONLY once per session (see
        // run-kickoff). On reload (already started) this resolves immediately
        // and we skip straight to polling, which replays from since=0. With no
        // stored message this tab never kicks off; it views the run instead.
        if (message) {
          await ensureRunStarted(sessionId, () =>
            startRun(appName, userId, sessionId, message)
          );
        }
        if (signal.aborted) return;

        // Seed session state once so the sidebar populates immediately, even for
        // keys set before any event and on reconnect/reload. (pollRun replays
        // from since=0 too, but this is more robust for pre-event state.)
        const seed = await getRunStatus(appName, userId, sessionId, 0);
        if (signal.aborted) return;

        // View mode (no stored message): only follow a run the server knows.
        // An empty log means nothing was ever kicked off for this session.
        if (!message && isUnstartedRun(seed)) {
          setStatus("error");
          setNotStarted(true);
          return;
        }
        if (seed.state) setSessionState((prev) => ({ ...prev, ...seed.state }));
        // The first poll result: from here the run is followed, not connecting.
        setStatus((s) => (s === "connecting" ? "running" : s));

        // Drain the poll to completion; the initial run fetches session state
        // on pause.
        const terminal = await consumePollEvents(
          pollRun(appName, userId, sessionId, { signal }),
          { syncOnPause: true }
        );
        if (!terminal) setStatus("completed");
      } catch (err) {
        // Unmount aborts the poll — that is not a real run failure.
        if (signal.aborted || (err instanceof Error && err.name === "AbortError")) {
          return;
        }
        failRun(err, "Agent run failed");
      }
    }

    run();

    return () => controller.abort();
  }, [appName, userId, sessionId, enabled, consumePollEvents, failRun]);

  // Reset the stall timer whenever a new event lands (keeps the per-event loop
  // bodies byte-identical — the timestamp is bumped here instead of inline).
  useEffect(() => {
    lastEventAt.current = Date.now();
  }, [events.length]);

  // Stall watchdog: while a run is "running", flag it as "stalled" if no event
  // has arrived for stallTimeoutFor(appName). Covers the orphaned-job case where
  // the poll would otherwise report "running" forever. Only armed while running,
  // so paused/completed/error/stalled runs are unaffected; a new event flips
  // "stalled" back to "running" (consumePollEvents), which re-arms it.
  const stallTimeout = stallTimeoutFor(appName);
  useEffect(() => {
    if (status !== "running") return;
    // Re-arm the baseline on entering "running" (e.g. resuming after a long
    // human-review pause) so we don't immediately flag a fresh run as stalled.
    lastEventAt.current = Date.now();
    const timer = setInterval(() => {
      if (Date.now() - lastEventAt.current > stallTimeout) {
        setStatus("stalled");
      }
    }, 10_000);
    return () => clearInterval(timer);
  }, [status, stallTimeout]);

  // Stale-tab watcher: while paused, re-check the run for an answer to this
  // checkpoint submitted elsewhere (another tab, or a reload that resumed it).
  // A paused page has stopped polling, so without this it would show "Waiting
  // for review" forever. Only detects; followRun owns the follow-up poll.
  useEffect(() => {
    if (status !== "paused" || !pauseContext) return;
    const { functionCallId } = pauseContext;
    let cancelled = false;
    let checking = false;
    let cursor = 0;
    const timer = setInterval(async () => {
      if (checking) return;
      checking = true;
      try {
        const res = await getRunStatus(appName, userId, sessionId, cursor);
        if (cancelled) return;
        cursor = res.nextCursor ?? cursor;
        if (!isPauseAnswered(res.events ?? [], functionCallId)) return;
        cancelled = true;
        clearInterval(timer);
        setPauseContext(null);
        setStatus("running");
        setNotice("This review was answered in another tab or window.");
        followRun();
      } catch {
        // Transient poll failure — keep waiting; the next tick retries.
      } finally {
        checking = false;
      }
    }, PAUSE_WATCH_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [status, pauseContext, appName, userId, sessionId, followRun]);

  // Abort any in-flight resume poll on unmount (mirrors the initial effect's
  // controller cleanup).
  useEffect(() => {
    return () => resumeAbortRef.current?.abort();
  }, []);

  // Resume from a paused long-running tool
  async function resume(response: Record<string, unknown>) {
    if (!pauseContext) return;
    setReviewErrors({});
    setStatus("running");
    setNotice(null);
    const ctx = pauseContext;
    setPauseContext(null);

    try {
      // Submit the human-review response; the server relaunches the detached
      // job. Then followRun re-enters pollRun (from since=0) to consume new
      // events, so a resume that hits the NEXT checkpoint pauses again and one
      // that finishes completes.
      // Checkpoint-3 sends its per-concept `edits` inside the response object;
      // split them out so they ride as the top-level resume `edits` field (merged
      // into session state server-side) rather than the LLM functionResponse.
      const { edits, ...fnResponse } = response as {
        edits?: Record<string, unknown>[];
      } & Record<string, unknown>;
      await resumeRun(
        appName,
        userId,
        sessionId,
        ctx.functionCallId,
        ctx.functionName,
        fnResponse,
        ctx.eventId,
        edits
      );
      // Accepted: this checkpoint's panel never comes back, so drop its drafts
      // (a rejected resume keeps them for the re-shown panel, below).
      clearDraftsFor(ctx.functionCallId);
    } catch (err) {
      // The server's duplicate-run guard rejected this resume WITHOUT applying
      // it (the previous segment was still finishing). The run is still paused
      // at the same checkpoint, but polling would not re-show the review panel
      // (the pause event is already deduped) — so restore it and let the user
      // re-submit, with a calm notice rather than a failure.
      if (err instanceof ResumeNotAppliedError) {
        // A 400 names the invalid fields; the panel shows them in place (its
        // draft survives the remount, see review-drafts.ts).
        setReviewErrors(err instanceof ResumeRejectedError ? err.fieldErrors : {});
        setPauseContext(ctx);
        setStatus("paused");
        setNotice(err.message);
        return;
      }
      failRun(err, "Resume failed");
      return;
    }
    await followRun();
  }

  // Continue a run that stopped early: post a new message to the same session
  // (the backend runs a new segment and the agent picks up from its history),
  // then follow it like a resume — to the next checkpoint or the end.
  async function continueRun() {
    setContinuing(true);
    setContinueError(null);
    setNotice(null);
    try {
      await startRun(appName, userId, sessionId, CONTINUE_MESSAGE);
    } catch (err) {
      setContinueError(err instanceof Error ? err.message : "Unknown error");
      setContinuing(false);
      return;
    }
    setStatus("running");
    setContinuing(false);
    await followRun();
  }

  return {
    events,
    status,
    errorMsg,
    notice,
    stepErrors,
    reviewErrors,
    sessionState,
    pauseContext,
    notStarted,
    continuing,
    continueError,
    resume,
    continueRun,
  };
}
