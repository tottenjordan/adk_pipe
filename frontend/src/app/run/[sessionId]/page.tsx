"use client";

import React, { useEffect, useState, useRef, useMemo, useCallback, use } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { EventLog } from "@/components/event-log";
import { TrendCards, parseTrendsMarkdown } from "@/components/trend-cards";
import { GcsWidget } from "@/components/gcs-widget";
import {
  startRun,
  pollRun,
  getRunStatus,
  resumeRun,
  getSession,
  getEventError,
} from "@/lib/api";
import { formatStateValue } from "@/lib/utils";
import { gcsProxyUrl, parseGsUri } from "@/lib/gcs";
import { hasStartedRun, markRunStarted } from "@/lib/run-kickoff";
import type { AgentEvent } from "@/lib/types";
import {
  RUN_STALL_TIMEOUT_MS,
  RUNSERVER_MARKER_AUTHOR,
  PIPELINE_STATE_KEYS,
} from "./run-config";
import { PipelineWidget } from "./run-widgets";
import { ReviewPanel } from "./ReviewPanel";

type Status = "running" | "completed" | "error" | "paused" | "stalled";

interface PauseContext {
  functionCallId: string;
  functionName: string;
  eventId: string;
}

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
  const [sessionState, setSessionState] = useState<Record<string, unknown>>({});
  const [toastDismissed, setToastDismissed] = useState(false);
  const [pauseContext, setPauseContext] = useState<PauseContext | null>(null);
  const startedRef = useRef(false);
  const seenEventIds = useRef(new Set<string>());
  const lastEventAt = useRef<number>(Date.now());
  // Aborts the resume poll loop on unmount (handleResume is a click handler,
  // not an effect, so it can't own an effect-scoped AbortController itself).
  const resumeAbortRef = useRef<AbortController | null>(null);

  const appName = searchParams.get("app") || "trend_scout";
  const userId = searchParams.get("userId") || "default_user";

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
  // long-running-tool pause detection). The only per-call differences are
  // captured by `opts`:
  //   - syncOnPause: fetch full session state on pause (the initial run does;
  //     the resume path relies on state already loaded).
  //   - stopOnPause: stop consuming as soon as a pause is detected (the resume
  //     path returns immediately; the initial run drains the generator).
  // Returns true when a terminal UI state (paused or error) was already set, so
  // the caller must not override it; false means the run completed normally.
  const consumePollEvents = useCallback(
    async (
      poll: AsyncGenerator<AgentEvent>,
      opts: { syncOnPause?: boolean; stopOnPause?: boolean } = {}
    ): Promise<boolean> => {
      let paused = false;
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

        // Detect long-running tool pause (interactive mode).
        // IMPORTANT: Skip partial (streaming) events — their function call
        // IDs are regenerated per chunk and won't match the session's final
        // event. Only capture pause context from the non-partial event.
        if (
          !event.partial &&
          event.longRunningToolIds &&
          event.longRunningToolIds.length > 0
        ) {
          const functionCalls = event.content?.parts
            ?.filter((p) => p.functionCall)
            ?.map((p) => p.functionCall!) ?? [];

          const pausedCall = functionCalls.find((fc) =>
            event.longRunningToolIds!.includes(fc.id ?? "")
          );

          if (pausedCall) {
            setPauseContext({
              functionCallId: pausedCall.id ?? "",
              functionName: pausedCall.name ?? "",
              eventId: event.id,
            });
            setStatus("paused");
            paused = true;
            // Fetch full session state so campaign metadata is available
            if (opts.syncOnPause) syncSessionState();
            if (opts.stopOnPause) return true;
          }
        }
      }
      return paused;
    },
    [syncSessionState]
  );

  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;

    const stored = sessionStorage.getItem(`run:${sessionId}`);
    const message = stored ? JSON.parse(stored).message : "";
    if (!message) {
      setStatus("error");
      setErrorMsg("No message found for this session. Please start a new run.");
      return;
    }

    const controller = new AbortController();
    const { signal } = controller;

    async function run() {
      try {
        // Kick off the detached background run — but ONLY once per session. The
        // run page remounts on every browser reload (startedRef resets), so
        // without this durable guard a reload would spawn a second detached run
        // (see run-kickoff). On reload we skip straight to polling, which
        // replays the existing run from since=0.
        if (!hasStartedRun(sessionId)) {
          await startRun(appName, userId, sessionId, message);
          markRunStarted(sessionId);
        }

        // Seed session state once so the sidebar populates immediately, even for
        // keys set before any event and on reconnect/reload. (pollRun replays
        // from since=0 too, but this is more robust for pre-event state.)
        const seed = await getRunStatus(appName, userId, sessionId, 0);
        if (seed.state) setSessionState((prev) => ({ ...prev, ...seed.state }));

        // Drain the poll to completion; the initial run fetches session state
        // on pause and keeps consuming (does not stop on the first pause).
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

  // Abort any in-flight resume poll on unmount (mirrors the initial effect's
  // controller cleanup — the resume path previously leaked its poll loop).
  useEffect(() => {
    return () => resumeAbortRef.current?.abort();
  }, []);

  // Resume from a paused long-running tool
  async function handleResume(response: Record<string, unknown>) {
    if (!pauseContext) return;
    setStatus("running");
    const ctx = pauseContext;
    setPauseContext(null);

    const controller = new AbortController();
    resumeAbortRef.current = controller;

    try {
      // Submit the human-review response; the server relaunches the detached
      // job. Then re-enter pollRun (from since=0) to consume new events —
      // seenEventIds dedup makes the replay idempotent, so a resume that hits
      // the NEXT checkpoint pauses again and one that finishes completes.
      await resumeRun(
        appName,
        userId,
        sessionId,
        ctx.functionCallId,
        ctx.functionName,
        response,
        ctx.eventId
      );

      // Resume stops at the first pause (returns immediately) and does not
      // re-sync session state — same as the former inline loop.
      const terminal = await consumePollEvents(
        pollRun(appName, userId, sessionId, { signal: controller.signal }),
        { stopOnPause: true }
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
  }

  // Fetch session state from backend to populate campaign metadata
  // that was set during a prior run/resume phase
  // Parse trend trawler output into clickable cards
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

  // Campaign metadata fields for left sidebar
  const campaignFields = useMemo(() => {
    const fields: { label: string; key: string; altKey?: string }[] = [
      { label: "Brand", key: "brand" },
      { label: "Target Audience", key: "target_audience" },
      { label: "Target Product", key: "target_product" },
      { label: "Key Selling Points", key: "key_selling_points" },
      { label: "Search Trend", key: "target_search_trends", altKey: "target_search_trend" },
    ];
    return fields
      .map((f) => ({
        ...f,
        value: formatStateValue(sessionState[f.key] ?? (f.altKey ? sessionState[f.altKey] : undefined)),
      }))
      .filter((f) => f.value);
  }, [sessionState]);

  // Pipeline state widgets — newest first
  const pipelineWidgets = useMemo(() => {
    return PIPELINE_STATE_KEYS.filter((p) => sessionState[p.key] != null);
  }, [sessionState]);

  const statusConfig: Record<Status, { color: string; glow: string }> = {
    running: { color: "bg-blue-500", glow: "shadow-blue-500/20" },
    paused: { color: "bg-amber-500", glow: "shadow-amber-500/20" },
    completed: { color: "bg-emerald-500", glow: "shadow-emerald-500/20" },
    error: { color: "bg-red-500", glow: "shadow-red-500/20" },
    stalled: { color: "bg-yellow-500", glow: "shadow-yellow-500/20" },
  };

  const showResultsToast =
    status === "completed" && appName !== "trend_scout" && !toastDismissed;

  // Right column content: pipeline widgets, GCS, research report, or trend cards
  const hasRightColumn =
    pipelineWidgets.length > 0 ||
    gcsUri ||
    researchReportUrl ||
    (status === "completed" && trends.length > 0);

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-8">
      {/* Page header */}
      <div className="mb-6 flex items-center justify-between animate-fadeIn">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">
            Agent Run
          </h1>
          <p className="mt-1 text-sm text-muted-foreground font-mono">
            {appName} / {sessionId}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {status === "completed" && appName !== "trend_scout" && (
            <Button
              size="lg"
              onClick={() => router.push(resultsUrl)}
              className="animate-fadeInUpSmooth"
            >
              View Results
            </Button>
          )}
        </div>
      </div>

      {errorMsg && (
        <div className="mb-4 rounded-xl bg-red-50 border border-red-200 px-5 py-4 animate-fadeIn">
          <p className="text-sm text-red-600">{errorMsg}</p>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
        {/* Left sidebar — campaign metadata only */}
        <div className="space-y-3 animate-fadeInUp animation-delay-100 opacity-0" style={{ animationFillMode: "forwards" }}>
          <h2 className="text-[10px] font-bold text-primary tracking-wider uppercase pt-1">
            Campaign Metadata
          </h2>
          {campaignFields.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">
              Waiting for metadata...
            </p>
          ) : (
            campaignFields.map((f) => (
              <div key={f.key} className="glass rounded-xl px-4 py-3 animate-fadeInUpSmooth">
                <dt className="text-[10px] font-bold uppercase tracking-wider text-indigo-500">
                  {f.label}
                </dt>
                <dd className="mt-1 text-sm font-medium leading-snug text-foreground">
                  {f.value}
                </dd>
              </div>
            ))
          )}
        </div>

        {/* Right content area */}
        <div className="animate-fadeInUp animation-delay-200 opacity-0" style={{ animationFillMode: "forwards" }}>
          <div
            className={`grid gap-6 ${hasRightColumn ? "lg:grid-cols-[1fr_380px]" : ""}`}
          >
            {/* Event stream */}
            <div className="glass rounded-2xl overflow-hidden">
              <div className="px-5 py-3 border-b border-border flex items-center justify-between">
                <h3 className="text-sm font-semibold text-foreground">
                  Event Stream
                </h3>
                <span className="text-[10px] text-muted-foreground tabular-nums">
                  {events.length} event{events.length !== 1 ? "s" : ""}
                </span>
              </div>
              <EventLog events={events} className="h-[600px]" />

              {/* Status bar */}
              <div className="border-t border-border px-5 py-3 flex items-center gap-2.5">
                <span
                  className={`inline-block h-2.5 w-2.5 rounded-full ${statusConfig[status].color} ${status === "running" ? "animate-pulse" : ""} shadow-lg ${statusConfig[status].glow}`}
                />
                {status === "running" && (
                  <span className="text-sm text-muted-foreground flex items-center gap-1.5">
                    Agent is processing
                    <span className="processing-dots text-blue-500">
                      <span />
                      <span />
                      <span />
                    </span>
                  </span>
                )}
                {status === "paused" && (
                  <span className="text-sm text-amber-600 font-medium">
                    Waiting for review
                  </span>
                )}
                {status === "completed" && (
                  <span className="text-sm text-emerald-600 font-medium">
                    Run completed
                  </span>
                )}
                {status === "error" && (
                  <span className="text-sm text-red-600 font-medium">
                    Run failed
                  </span>
                )}
                {status === "stalled" && (
                  <span className="text-sm text-yellow-700 font-medium">
                    Run may have stalled — no activity for a while. It may still
                    be running in the background; reload to reconnect.
                  </span>
                )}
              </div>
            </div>

            {/* Review panel for interactive mode */}
            {status === "paused" && pauseContext && (
              <div className="lg:col-span-full">
                <ReviewPanel
                  functionName={pauseContext.functionName}
                  sessionState={sessionState}
                  onResume={handleResume}
                />
              </div>
            )}

            {/* Right column: GCS output, research report, pipeline widgets, trend cards */}
            {hasRightColumn && (
              <div className="space-y-3 max-h-[700px] overflow-y-auto">
                {/* Cloud Storage Output */}
                {gcsUri && <GcsWidget uri={gcsUri} />}

                {/* Research Report */}
                {researchReportUrl && (
                  <div className="glass rounded-xl px-4 py-3 animate-fadeInUpSmooth">
                    <dt className="text-[10px] font-bold uppercase tracking-wider text-amber-600">
                      Research Report
                    </dt>
                    <dd className="mt-1.5">
                      <a
                        href={researchReportUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-sm font-medium text-primary hover:text-primary/80 hover:underline transition-colors"
                      >
                        <svg
                          className="h-4 w-4 shrink-0"
                          fill="none"
                          viewBox="0 0 24 24"
                          stroke="currentColor"
                          strokeWidth={2}
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                          />
                        </svg>
                        View PDF Report
                      </a>
                    </dd>
                  </div>
                )}

                {/* Trend cards — only for completed trend_scout runs */}
                {status === "completed" && trends.length > 0 && (
                  <TrendCards trends={trends} campaignState={sessionState} />
                )}

                {/* Pipeline state widgets — newest at top */}
                {pipelineWidgets.map((p) => (
                  <PipelineWidget
                    key={p.key}
                    label={p.label}
                    stateKey={p.key}
                    data={sessionState[p.key]}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Floating toast to guide user to results page */}
      {showResultsToast && (
        <div className="fixed bottom-6 right-6 z-50 animate-in slide-in-from-bottom-4 fade-in duration-300">
          <div className="glass-strong rounded-2xl shadow-xl shadow-black/8 flex items-center gap-4 px-5 py-4">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-emerald-50">
              <span className="inline-block h-3 w-3 rounded-full bg-emerald-500 shadow-lg shadow-emerald-500/20" />
            </div>
            <div className="mr-2">
              <p className="text-sm font-semibold text-foreground">
                Run complete!
              </p>
              <p className="text-xs text-muted-foreground">
                View results here
              </p>
            </div>
            <Button size="sm" onClick={() => router.push(resultsUrl)}>
              View Results
            </Button>
            <button
              onClick={() => setToastDismissed(true)}
              className="ml-1 flex h-6 w-6 items-center justify-center rounded-lg text-muted-foreground hover:text-foreground hover:bg-black/5 transition-colors"
              aria-label="Dismiss"
            >
              &times;
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
