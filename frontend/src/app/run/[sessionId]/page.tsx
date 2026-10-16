"use client";

import React, { useMemo, use } from "react";
import Link from "next/link";
import { useSearchParams, useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { formatEventTime } from "@/components/event-log";
import { TrendCards, parseTrendsMarkdown } from "@/components/trend-cards";
import { GcsWidget } from "@/components/gcs-widget";
import { CreativeBriefOutput } from "@/components/creative-brief";
import { parseCreativeBrief } from "@/lib/creative-brief";
import type { ReportSources } from "@/lib/research-report";
import { FileDown } from "lucide-react";
import { agentLabel, isCreativeAgent } from "@/lib/agents";
import {
  answeredReviewNames,
  currentStage,
  deriveStages,
  type RunStageStatus,
} from "@/lib/run-stages";
import { stoppedEarly } from "@/lib/run-completion";
import { SELF_USER_ID } from "@/lib/api";
import { STEP_ERROR_PREFIX } from "@/lib/step-errors";
import { LearningSummary } from "@/components/learning-summary";
import { TrendSituationLine } from "@/components/trend-situation-line";
import { SaferAngleLine } from "@/components/safer-angle-line";
import { learningSummaryText } from "@/lib/rating-learning";
import { trendSituationText } from "@/lib/trend-situation";
import {
  buildDisplayFields,
  RATING_LEARNING_FIELD,
  SAFER_ANGLE_FIELD,
  VISUAL_DIRECTION_FIELDS,
  type DisplayFieldDef,
} from "@/lib/utils";
import { gcsProxyUrl, parseGsUri } from "@/lib/gcs";
import { PIPELINE_STATE_KEYS } from "./run-config";
import { useRunFollower, type RunStatus } from "./use-run-follower";
import { PipelineWidget } from "./run-widgets";
import { StageSpine } from "./stage-spine";
import { BriefSummary, ConnectionLostBanner, CurrentStagePanel, TechnicalLog } from "./run-sections";
import { ReviewPanel } from "./ReviewPanel";

const CAMPAIGN_FIELD_DEFS: DisplayFieldDef[] = [
  { label: "Brand", key: "brand" },
  { label: "Target audience", key: "target_audience" },
  { label: "Target product", key: "target_product" },
  { label: "Key selling points", key: "key_selling_points" },
  { label: "Search trend", key: "target_search_trends", altKey: "target_search_trend" },
  RATING_LEARNING_FIELD,
  SAFER_ANGLE_FIELD,
];

export default function RunPage({ params }: PageProps<"/run/[sessionId]">) {
  const { sessionId } = use(params);
  const searchParams = useSearchParams();
  const router = useRouter();
  const appName = searchParams.get("app") || "trend_scout";
  const userId = searchParams.get("userId") || SELF_USER_ID;
  const {
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
    resume: handleResume,
    continueRun: handleContinue,
  } = useRunFollower(appName, userId, sessionId);

  const resultsUrl = useMemo(() => {
    const p = new URLSearchParams({ app: appName, userId });
    return `/results/${sessionId}?${p.toString()}`;
  }, [appName, userId, sessionId]);

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

  // Structured creative brief (creative agents), once the brief writer has run
  const creativeBrief = useMemo(
    () => parseCreativeBrief(sessionState.creative_brief),
    [sessionState.creative_brief]
  );

  // Pipeline outputs, in pipeline order
  const pipelineWidgets = useMemo(() => {
    return PIPELINE_STATE_KEYS.filter((p) => sessionState[p.key] != null);
  }, [sessionState]);

  // Stage spine (pure derivation from state + pause + status). The stage views
  // know no "disconnected": like "stalled", the run may still be going. Nor
  // "connecting": until the first poll answers, they show the run starting.
  const stageStatus: RunStageStatus =
    status === "disconnected" ? "stalled" : status === "connecting" ? "running" : status;
  const answeredReviews = useMemo(() => answeredReviewNames(events), [events]);
  const stages = useMemo(
    () => deriveStages(appName, sessionState, pauseContext, stageStatus, answeredReviews),
    [appName, sessionState, pauseContext, stageStatus, answeredReviews]
  );
  const current = useMemo(() => currentStage(stages), [stages]);

  // The segment ended but the workflow never wrote its final key (e.g. the root
  // model returned an empty turn). Derived for display only — the page's own
  // `status` stays "completed" until the user continues the run.
  const stopped = useMemo(
    () => stoppedEarly(appName, sessionState, stageStatus, pauseContext, answeredReviews),
    [appName, sessionState, stageStatus, pauseContext, answeredReviews]
  );


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

  const statusLine: Record<RunStatus, { text: string; dot: string; tone: string }> = {
    connecting: { text: "Connecting…", dot: "bg-muted-foreground", tone: "text-muted-foreground" },
    running: { text: "Running", dot: "bg-primary", tone: "text-foreground" },
    paused: { text: "Waiting for your review", dot: "bg-mark-pending", tone: "text-mark-pending" },
    completed: { text: "Completed", dot: "bg-mark-pass", tone: "text-mark-pass" },
    error: {
      text: notStarted ? "Not started" : "Failed",
      dot: "bg-mark-fail",
      tone: "text-mark-fail",
    },
    stalled: { text: "No recent activity", dot: "bg-mark-pending", tone: "text-mark-pending" },
    disconnected: { text: "Connection lost", dot: "bg-mark-pending", tone: "text-mark-pending" },
  };

  const statusDisplay = stopped
    ? { text: `Stopped before ${stopped.stage}`, dot: "bg-mark-pending", tone: "text-mark-pending" }
    : statusLine[status];

  const learningText = learningSummaryText(sessionState);
  const situationText = trendSituationText(sessionState);
  const hasOutputs =
    pipelineWidgets.length > 0 ||
    gcsUri ||
    researchReportUrl ||
    creativeBrief ||
    learningText ||
    situationText;

  return (
    <div className="mx-auto max-w-[1400px] px-4 py-6 sm:px-6 sm:py-8">
      {/* Page header */}
      <div className="mb-4 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold text-foreground">{agentLabel(appName)}</h1>
          <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
            <span role="status" aria-label="Run status" className="inline-flex items-center gap-x-2">
              <span className={`inline-block h-2 w-2 rounded-full ${statusDisplay.dot}`} aria-hidden />
              <span className={`font-medium ${statusDisplay.tone}`}>{statusDisplay.text}</span>
            </span>
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

      {stepErrors.length > 0 && (
        <div className="mb-4 rounded-lg border border-border bg-muted/40 px-5 py-3">
          {stepErrors.map((msg) => (
            <p key={msg} className="text-sm text-muted-foreground">
              {STEP_ERROR_PREFIX}
              {msg}
            </p>
          ))}
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

      {status === "disconnected" && (
        <ConnectionLostBanner onReload={() => window.location.reload()} />
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
            <StageSpine stages={stages} current={current} runStatus={stageStatus} />
          </div>

          {/* Main area */}
          <div className="min-w-0 space-y-4">
            {status === "paused" && pauseContext ? (
              <ReviewPanel
                // A fresh panel per checkpoint call: checkpoint 2 can pause
                // twice on review_ad_copies (before and after the revision).
                key={pauseContext.functionCallId}
                functionCallId={pauseContext.functionCallId}
                serverErrors={reviewErrors}
                functionName={pauseContext.functionName}
                sessionState={sessionState}
                onResume={handleResume}
              />
            ) : (
              <CurrentStagePanel
                stage={current}
                stages={stages}
                status={stageStatus}
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
                  {/* What rating-driven learning applied (opt-in runs only) */}
                  <LearningSummary state={sessionState} className="sm:col-span-2" />

                  {/* The trend's validated current moment (read-only) */}
                  <TrendSituationLine state={sessionState} className="sm:col-span-2" />
                  {/* The respectful angle of an opted-in sensitive-trend run */}
                  <SaferAngleLine state={sessionState} className="sm:col-span-2" />

                  {/* Full width first: the brief is the strategy every later output follows */}
                  {creativeBrief && (
                    <CreativeBriefOutput
                      brief={creativeBrief}
                      sources={sessionState.sources as ReportSources | undefined}
                      className="sm:col-span-2"
                    />
                  )}

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
