"use client";

import React, { useEffect, useState, useRef, useMemo, useCallback, use } from "react";
import Link from "next/link";
import { useSearchParams, useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { formatEventTime } from "@/components/event-log";
import { TrendCards, parseTrendsMarkdown } from "@/components/trend-cards";
import { GcsWidget } from "@/components/gcs-widget";
import { FileDown } from "lucide-react";
import { agentLabel, isCreativeAgent } from "@/lib/agents";
import { answeredReviewNames, currentStage, deriveStages } from "@/lib/run-stages";
import { CONTINUE_MESSAGE, stoppedEarly } from "@/lib/run-completion";
import {
  startRun,
  pollRun,
  getRunStatus,
  resumeRun,
  ResumeNotAppliedError,
  getSession,
  getEventError,
  SELF_USER_ID,
} from "@/lib/api";
import {
  buildDisplayFields,
  VISUAL_DIRECTION_FIELDS,
  type DisplayFieldDef,
} from "@/lib/utils";
import { gcsProxyUrl, parseGsUri } from "@/lib/gcs";
import { ensureRunStarted, isUnstartedRun, readRunMessage } from "@/lib/run-kickoff";
import type { AgentEvent } from "@/lib/types";
import {
  PendingLongRunningCalls,
  isPauseAnswered,
  type PauseContext,
} from "@/lib/pause-detection";
import {
  RUN_STALL_TIMEOUT_MS,
  PAUSE_WATCH_INTERVAL_MS,
  RUNSERVER_MARKER_AUTHOR,
  PIPELINE_STATE_KEYS,
} from "./run-config";
import { PipelineWidget } from "./run-widgets";
import { StageSpine } from "./stage-spine";
import { BriefSummary, CurrentStagePanel, TechnicalLog } from "./run-sections";
import { ReviewPanel } from "./ReviewPanel";

const CAMPAIGN_FIELD_DEFS: DisplayFieldDef[] = [
  { label: "Brand", key: "brand" },
  { label: "Target audience", key: "target_audience" },
  { label: "Target product", key: "target_product" },
  { label: "Key selling points", key: "key_selling_points" },
  { label: "Search trend", key: "target_search_trends", altKey: "target_search_trend" },
];

type Status = "running" | "completed" | "error" | "paused" | "stalled";

export default function RunPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = use(params);
  const searchParams = useSearchParams();
  const router = useRouter();
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [status, setStatus] = useState<Status>("running");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  // Non-error, informational notice (e.g. a resume that must be re-submitted).
  const [notice, setNotice] = useState<string | null>(null);
  const [sessionState, setSessionState] = useState<Record<string, unknown>>({});
  const [pauseContext, setPauseContext] = useState<PauseContext | null>(null);
  // Viewing a session the server has no run for (and no kick-off message).
  const [notStarted, setNotStarted] = useState(false);
  const seenEventIds = useRef(new Set<string>());
  const lastEventAt = useRef<number>(Date.now());
  // Aborts the resume poll loop on unmount (handleResume is a click handler,
  // not an effect, so it can't own an effect-scoped AbortController itself).
  const resumeAbortRef = useRef<AbortController | null>(null);
  // "Continue run" for a run that stopped early: in flight, and its kick-off error.
  const [continuing, setContinuing] = useState(false);
  const [continueError, setContinueError] = useState<string | null>(null);

  const appName = searchParams.get("app") || "trend_scout";
  const userId = searchParams.get("userId") || SELF_USER_ID;

  const resultsUrl = useMemo(() => {
    const p = new URLSearchParams({ app: appName, userId });
    return `/results/${sessionId}?${p.toString()}`;
  }, [appName, userId, sessionId]);

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
  // path. This is the single copy of what used to be two byte-identical loops
  // (event-id dedup, error surfacing, setEvents, state-delta merge, and
  // long-running-tool pause detection). The only per-call difference is
  // captured by `opts`:
  //   - syncOnPause: fetch full session state on pause (the initial run does;
  //     the resume path relies on state already loaded).
  // The pause is decided when the poll segment ENDS (a paused segment ends
  // right after its checkpoint call, with a terminal marker): it is the
  // long-running call still unanswered. A long-running call alone is not a
  // pause — pipeline NodeTools are long-running too but are answered in the
  // same segment (see PendingLongRunningCalls).
  // Returns true when a terminal UI state (paused or error) was already set, so
  // the caller must not override it; false means the run completed normally.
  const consumePollEvents = useCallback(
    async (
      poll: AsyncGenerator<AgentEvent>,
      opts: { syncOnPause?: boolean } = {}
    ): Promise<boolean> => {
      const pending = new PendingLongRunningCalls();
      for await (const event of poll) {
        // Skip the server's internal run-status marker events — status/error
        // come from the poll payload, not these (see RUNSERVER_MARKER_AUTHOR).
        if (event.author === RUNSERVER_MARKER_AUTHOR) continue;

        // Deduplicate events by ID
        if (event.id && seenEventIds.current.has(event.id)) continue;
        if (event.id) seenEventIds.current.add(event.id);

        // Surface backend failure events (e.g. a model 429) — these arrive as
        // data events with no content, so a content-only loop would drop them
        // and the run would look like a silent stall.
        const evErr = getEventError(event);
        if (evErr) {
          setStatus("error");
          setErrorMsg(evErr);
          return true;
        }

        setEvents((prev) => [...prev, event]);

        if (event.actions?.stateDelta) {
          setSessionState((prev) => ({
            ...prev,
            ...event.actions!.stateDelta,
          }));
        }

        pending.observe(event);
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
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : "Resume failed");
    }
  }, [appName, userId, sessionId, consumePollEvents]);

  useEffect(() => {
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
        setStatus("error");
        setErrorMsg(
          err instanceof Error ? err.message : "Agent run failed"
        );
      }
    }

    run();

    return () => controller.abort();
  }, [appName, userId, sessionId, consumePollEvents]);

  // Reset the stall timer whenever a new event lands (keeps the per-event loop
  // bodies byte-identical — the timestamp is bumped here instead of inline).
  useEffect(() => {
    lastEventAt.current = Date.now();
  }, [events.length]);

  // Stall watchdog: while a run is "running", flag it as "stalled" if no event
  // has arrived for RUN_STALL_TIMEOUT_MS. Covers the orphaned-job case where the
  // poll would otherwise report "running" forever. Only armed while running, so
  // paused/completed/error/stalled runs are unaffected.
  useEffect(() => {
    if (status !== "running") return;
    // Re-arm the baseline on entering "running" (e.g. resuming after a long
    // human-review pause) so we don't immediately flag a fresh run as stalled.
    lastEventAt.current = Date.now();
    const timer = setInterval(() => {
      if (Date.now() - lastEventAt.current > RUN_STALL_TIMEOUT_MS) {
        setStatus("stalled");
      }
    }, 10_000);
    return () => clearInterval(timer);
  }, [status]);

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
  // controller cleanup — the resume path previously leaked its poll loop).
  useEffect(() => {
    return () => resumeAbortRef.current?.abort();
  }, []);

  // Resume from a paused long-running tool
  async function handleResume(response: Record<string, unknown>) {
    if (!pauseContext) return;
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
    } catch (err) {
      // The server's duplicate-run guard rejected this resume WITHOUT applying
      // it (the previous segment was still finishing). The run is still paused
      // at the same checkpoint, but polling would not re-show the review panel
      // (the pause event is already deduped) — so restore it and let the user
      // re-submit, with a calm notice rather than a failure.
      if (err instanceof ResumeNotAppliedError) {
        setPauseContext(ctx);
        setStatus("paused");
        setNotice(err.message);
        return;
      }
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : "Resume failed");
      return;
    }
    await followRun();
  }

  // Parse trend_scout output into clickable cards
  const trends = useMemo(() => {
    const selectedGtrends = sessionState.selected_gtrends;
    if (appName !== "trend_scout" || typeof selectedGtrends !== "string")
      return [];
    return parseTrendsMarkdown(selectedGtrends);
  }, [appName, sessionState.selected_gtrends]);

  // Build research report proxy URL from state
  const researchReportUrl = useMemo(() => {
    const parsed = parseGsUri(sessionState.research_report_gcs_uri);
    return parsed ? gcsProxyUrl(parsed.bucket, parsed.path) : "";
  }, [sessionState.research_report_gcs_uri]);

  // Build GCS URI from state
  const gcsUri = useMemo(() => {
    const parts = [
      sessionState.gcs_bucket,
      sessionState.gcs_folder,
      sessionState.agent_output_dir,
    ].filter(Boolean);
    return parts.length >= 2 ? parts.join("/") : "";
  }, [
    sessionState.gcs_bucket,
    sessionState.gcs_folder,
    sessionState.agent_output_dir,
  ]);

  // Campaign metadata fields for the brief summary
  const campaignFields = useMemo(
    () => buildDisplayFields(sessionState, CAMPAIGN_FIELD_DEFS),
    [sessionState],
  );

  // Optional user visual art-direction inputs (PR #114) — hidden when unset
  const visualDirectionFields = useMemo(
    () => buildDisplayFields(sessionState, VISUAL_DIRECTION_FIELDS),
    [sessionState],
  );

  // Pipeline outputs, in pipeline order
  const pipelineWidgets = useMemo(() => {
    return PIPELINE_STATE_KEYS.filter((p) => sessionState[p.key] != null);
  }, [sessionState]);

  // Stage spine (pure derivation from state + pause + status)
  const answeredReviews = useMemo(() => answeredReviewNames(events), [events]);
  const stages = useMemo(
    () => deriveStages(appName, sessionState, pauseContext, status, answeredReviews),
    [appName, sessionState, pauseContext, status, answeredReviews]
  );
  const current = useMemo(() => currentStage(stages), [stages]);

  // The segment ended but the workflow never wrote its final key (e.g. the root
  // model returned an empty turn). Derived for display only — the page's own
  // `status` stays "completed" until the user continues the run.
  const stopped = useMemo(
    () => stoppedEarly(appName, sessionState, status, pauseContext, answeredReviews),
    [appName, sessionState, status, pauseContext, answeredReviews]
  );

  // Continue a run that stopped early: post a new message to the same session
  // (the backend runs a new segment and the agent picks up from its history),
  // then follow it like a resume — to the next checkpoint or the end.
  async function handleContinue() {
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

  // Local time of the newest event (events arrive in order).
  const lastUpdate = formatEventTime(events[events.length - 1]?.timestamp);

  const showResults = status === "completed" && isCreativeAgent(appName);
  // A stopped run's primary action is "Continue run"; its results are partial.
  const resultsButton = showResults ? (
    stopped ? (
      <Button size="lg" variant="outline" onClick={() => router.push(resultsUrl)}>
        View partial results
      </Button>
    ) : (
      <Button size="lg" onClick={() => router.push(resultsUrl)}>
        View results
      </Button>
    )
  ) : null;

  const statusLine: Record<Status, { text: string; dot: string; tone: string }> = {
    running: { text: "Running", dot: "bg-primary", tone: "text-foreground" },
    paused: { text: "Waiting for your review", dot: "bg-mark-pending", tone: "text-mark-pending" },
    completed: { text: "Completed", dot: "bg-mark-pass", tone: "text-mark-pass" },
    error: {
      text: notStarted ? "Not started" : "Failed",
      dot: "bg-mark-fail",
      tone: "text-mark-fail",
    },
    stalled: { text: "No recent activity", dot: "bg-mark-pending", tone: "text-mark-pending" },
  };

  const statusDisplay = stopped
    ? { text: `Stopped before ${stopped.stage}`, dot: "bg-mark-pending", tone: "text-mark-pending" }
    : statusLine[status];

  const hasOutputs = pipelineWidgets.length > 0 || gcsUri || researchReportUrl;

  return (
    <div className="mx-auto max-w-[1400px] px-4 py-6 sm:px-6 sm:py-8">
      {/* Page header */}
      <div className="mb-4 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold text-foreground">{agentLabel(appName)}</h1>
          <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
            <span className={`inline-block h-2 w-2 rounded-full ${statusDisplay.dot}`} aria-hidden />
            <span className={`font-medium ${statusDisplay.tone}`}>{statusDisplay.text}</span>
            <span className="ml-2 min-w-0 truncate font-mono text-xs text-muted-foreground" title={sessionId}>
              {sessionId}
            </span>
          </p>
        </div>
        {resultsButton}
      </div>

      {!notStarted && (
        <div className="mb-6">
          <BriefSummary
            campaignFields={campaignFields}
            visualDirectionFields={visualDirectionFields}
          />
        </div>
      )}

      {notice && (
        <div className="mb-4 rounded-lg border border-mark-pending/40 bg-mark-pending/10 px-5 py-4">
          <p className="text-sm text-mark-pending">{notice}</p>
        </div>
      )}

      {notStarted && (
        <div className="mb-4 rounded-lg border border-mark-fail/40 bg-mark-fail/5 px-5 py-4">
          <p className="text-sm text-mark-fail">
            This session was created but its run never started. Start a new run from{" "}
            <Link href="/" className="font-medium underline underline-offset-2">
              New run
            </Link>
            , or reuse the brief with Duplicate in{" "}
            <Link href="/runs" className="font-medium underline underline-offset-2">
              Runs
            </Link>
            .
          </p>
        </div>
      )}

      {errorMsg && (
        <div role="alert" className="mb-4 rounded-lg border border-mark-fail/40 bg-mark-fail/5 px-5 py-4">
          <p className="text-sm font-medium text-mark-fail">{errorMsg}</p>
          <p className="mt-1 text-sm text-foreground">
            The technical log below shows the last step that ran. To try again, use Duplicate in{" "}
            <Link href="/runs" className="font-medium text-primary underline-offset-4 hover:underline">
              Runs
            </Link>{" "}
            to start a new run with the same brief.
          </p>
        </div>
      )}

      {!notStarted && (
        <div className="grid gap-6 lg:grid-cols-[220px_minmax(0,1fr)]">
          {/* Stage spine (vertical on lg, compact step bar below) */}
          <div className="min-w-0 lg:pt-1">
            <StageSpine stages={stages} current={current} runStatus={status} />
          </div>

          {/* Main area */}
          <div className="min-w-0 space-y-4">
            {status === "paused" && pauseContext ? (
              <ReviewPanel
                functionName={pauseContext.functionName}
                sessionState={sessionState}
                onResume={handleResume}
              />
            ) : (
              <CurrentStagePanel
                stage={current}
                stages={stages}
                status={status}
                lastUpdate={lastUpdate}
                stopped={
                  stopped
                    ? {
                        stage: stopped.stage,
                        onContinue: handleContinue,
                        continuing,
                        error: continueError,
                      }
                    : null
                }
              />
            )}

            {/* Trend cards — only for completed trend_scout runs */}
            {status === "completed" && trends.length > 0 && (
              <TrendCards trends={trends} campaignState={sessionState} />
            )}

            {hasOutputs && (
              <section aria-labelledby="run-outputs-heading">
                <h2 id="run-outputs-heading" className="mb-2 text-sm font-semibold text-foreground">
                  {status === "completed" && !stopped ? "Outputs" : "Outputs so far"}
                </h2>
                <div className="grid gap-3 sm:grid-cols-2">
                  {researchReportUrl && (
                    <a
                      href={researchReportUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="block rounded-lg border border-border bg-card px-4 py-3 transition-colors hover:border-primary/40"
                    >
                      <span className="block text-xs font-medium text-muted-foreground">
                        Research report
                      </span>
                      <span className="mt-0.5 flex items-center gap-1.5 text-sm font-medium text-primary">
                        <FileDown className="h-4 w-4 shrink-0" aria-hidden />
                        Open PDF report
                      </span>
                    </a>
                  )}

                  {pipelineWidgets.map((p) => (
                    <PipelineWidget
                      key={p.key}
                      label={p.label}
                      stateKey={p.key}
                      noun={p.noun}
                      data={sessionState[p.key]}
                    />
                  ))}

                  {gcsUri && (
                    <div className="sm:col-span-2">
                      <GcsWidget uri={gcsUri} />
                    </div>
                  )}
                </div>
              </section>
            )}

            <TechnicalLog events={events} />
          </div>
        </div>
      )}
    </div>
  );
}
