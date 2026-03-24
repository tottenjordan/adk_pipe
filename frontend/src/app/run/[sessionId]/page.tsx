"use client";

import { useEffect, useState, useRef, useMemo, use } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EventLog } from "@/components/event-log";
import { TrendCards, parseTrendsMarkdown } from "@/components/trend-cards";
import { GcsWidget } from "@/components/gcs-widget";
import { streamRun } from "@/lib/api";
import type { AgentEvent } from "@/lib/types";

type Status = "running" | "completed" | "error";

/** Pipeline state keys to surface as collapsible widgets, in display order (newest first). */
const PIPELINE_STATE_KEYS = [
  { key: "final_visual_concepts", label: "Final Visual Concepts" },
  { key: "visual_concept_critique", label: "Visual Concept Critique" },
  { key: "visual_draft", label: "Visual Draft" },
  { key: "ad_copy_critique", label: "Ad Copy Critique" },
  { key: "ad_copy_draft", label: "Ad Copy Draft" },
];

/** Human-readable labels for schema field keys. */
const FIELD_LABELS: Record<string, string> = {
  id: "ID",
  original_id: "ID",
  ad_copy_id: "Ad Copy ID",
  tone_style: "Tone / Style",
  headline: "Headline",
  body_text: "Body Text",
  trend_connection: "Trend Connection",
  audience_appeal_rationale: "Audience Appeal",
  audience_appeal: "Audience Appeal",
  social_caption: "Social Caption",
  call_to_action: "Call to Action",
  detailed_performance_rationale: "Performance Rationale",
  selection_rationale: "Selection Rationale",
  concept_name: "Concept Name",
  trend_visual_link: "Trend Visual Link",
  trend: "Trend",
  trend_reference: "Trend Reference",
  markets_product: "Markets Product",
  concept_summary: "Concept Summary",
  image_generation_prompt: "Image Prompt",
  critique_summary: "Critique Summary",
};

/** Extract the list of items from pipeline state data (dict with one key holding an array). */
function extractItems(data: unknown): Record<string, unknown>[] | null {
  if (!data || typeof data !== "object") return null;
  const obj = data as Record<string, unknown>;
  const keys = Object.keys(obj);
  for (const k of keys) {
    if (Array.isArray(obj[k])) return obj[k] as Record<string, unknown>[];
  }
  return null;
}

/** Render a single item card with clearly labeled fields. */
function ItemCard({ item, index }: { item: Record<string, unknown>; index: number }) {
  const entries = Object.entries(item).filter(
    ([, v]) => v !== null && v !== undefined && v !== ""
  );
  // Find a good title: concept_name, headline, or index
  const title =
    (item.concept_name as string) ||
    (item.headline as string) ||
    `Item ${index + 1}`;

  return (
    <div className="rounded border border-border bg-card px-3 py-2 space-y-1.5">
      <div className="flex items-center gap-1.5 border-b border-border pb-1.5">
        <Badge variant="secondary" className="text-[10px] px-1.5 py-0">
          {index + 1}
        </Badge>
        <span className="text-xs font-semibold leading-tight">{title}</span>
      </div>
      {entries.map(([key, value]) => {
        if (key === "concept_name" || key === "headline") return null;
        const label = FIELD_LABELS[key] || key.replace(/_/g, " ");
        return (
          <div key={key}>
            <dt className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              {label}
            </dt>
            <dd className="text-xs leading-snug break-words whitespace-pre-wrap">
              {typeof value === "string" || typeof value === "number"
                ? String(value)
                : JSON.stringify(value, null, 2)}
            </dd>
          </div>
        );
      })}
    </div>
  );
}

function PipelineWidget({
  label,
  data,
}: {
  label: string;
  data: unknown;
}) {
  const [open, setOpen] = useState(false);
  const items = extractItems(data);
  const itemCount = items ? items.length : 0;

  return (
    <>
      <Card
        className="shadow-sm animate-in fade-in slide-in-from-top-2 duration-300 cursor-pointer hover:bg-muted/50 transition-colors"
        onClick={() => setOpen(true)}
      >
        <CardHeader className="py-2.5 px-4">
          <CardTitle className="text-sm flex items-center justify-between">
            <span className="flex items-center gap-2">
              <span className="inline-block h-2 w-2 rounded-full bg-green-500" />
              {label}
              {itemCount > 0 && (
                <Badge variant="secondary" className="text-[10px] px-1.5 py-0">
                  {itemCount}
                </Badge>
              )}
            </span>
            <span className="text-xs text-muted-foreground font-normal">
              click to view
            </span>
          </CardTitle>
        </CardHeader>
      </Card>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm animate-in fade-in duration-150"
          onClick={() => setOpen(false)}
        >
          <div
            className="relative mx-4 w-full max-w-3xl max-h-[85vh] overflow-hidden flex flex-col rounded-lg border border-border bg-background shadow-xl animate-in zoom-in-95 duration-200"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-border px-5 py-4">
              <h2 className="text-base font-semibold flex items-center gap-2">
                <span className="inline-block h-2.5 w-2.5 rounded-full bg-green-500" />
                {label}
                {itemCount > 0 && (
                  <Badge variant="secondary">{itemCount} items</Badge>
                )}
              </h2>
              <button
                onClick={() => setOpen(false)}
                className="text-muted-foreground hover:text-foreground text-lg leading-none"
                aria-label="Close"
              >
                &times;
              </button>
            </div>
            <div className="flex-1 overflow-auto p-5">
              {items ? (
                <div className="space-y-2.5">
                  {items.map((item, i) => (
                    <ItemCard key={i} item={item} index={i} />
                  ))}
                </div>
              ) : (
                <pre className="whitespace-pre-wrap break-words rounded bg-muted p-4 text-sm font-mono leading-relaxed">
                  {typeof data === "string" ? data : JSON.stringify(data, null, 2)}
                </pre>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
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
  const startedRef = useRef(false);

  const appName = searchParams.get("app") || "trend_trawler";
  const userId = searchParams.get("userId") || "default_user";

  const resultsUrl = useMemo(() => {
    const p = new URLSearchParams({ app: appName, userId });
    return `/results/${sessionId}?${p.toString()}`;
  }, [appName, userId, sessionId]);

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

    async function run() {
      try {
        for await (const event of streamRun(
          appName,
          userId,
          sessionId,
          message
        )) {
          setEvents((prev) => [...prev, event]);

          if (event.actions?.stateDelta) {
            setSessionState((prev) => ({
              ...prev,
              ...event.actions!.stateDelta,
            }));
          }
        }
        setStatus("completed");
      } catch (err) {
        setStatus("error");
        setErrorMsg(
          err instanceof Error ? err.message : "Agent run failed"
        );
      }
    }

    run();
  }, [appName, userId, sessionId]);

  // Parse trend trawler output into clickable cards
  const trends = useMemo(() => {
    const selectedGtrends = sessionState.selected_gtrends;
    if (appName !== "trend_trawler" || typeof selectedGtrends !== "string")
      return [];
    return parseTrendsMarkdown(selectedGtrends);
  }, [appName, sessionState.selected_gtrends]);

  // Build research report proxy URL from state
  const researchReportUrl = useMemo(() => {
    const uri = sessionState.research_report_gcs_uri;
    if (typeof uri !== "string" || !uri.startsWith("gs://")) return "";
    const withoutPrefix = uri.replace(/^gs:\/\//, "");
    const slashIdx = withoutPrefix.indexOf("/");
    if (slashIdx < 0) return "";
    const bucket = withoutPrefix.slice(0, slashIdx);
    const path = withoutPrefix.slice(slashIdx + 1);
    return `/api/gcs?bucket=${encodeURIComponent(bucket)}&path=${encodeURIComponent(path)}`;
  }, [sessionState.research_report_gcs_uri]);

  // Build GCS URI from state
  const gcsUri = useMemo(() => {
    const parts = [
      sessionState.gcs_bucket,
      sessionState.gcs_folder,
      sessionState.agent_output_dir,
    ].filter(Boolean);
    return parts.length >= 2 ? parts.join("/") : "";
  }, [sessionState.gcs_bucket, sessionState.gcs_folder, sessionState.agent_output_dir]);

  // Campaign metadata fields for left sidebar
  const campaignFields = useMemo(() => {
    const fields: { label: string; key: string }[] = [
      { label: "Brand", key: "brand" },
      { label: "Target Audience", key: "target_audience" },
      { label: "Target Product", key: "target_product" },
      { label: "Key Selling Points", key: "key_selling_points" },
      { label: "Search Trend", key: "target_search_trends" },
    ];
    return fields
      .map((f) => ({ ...f, value: sessionState[f.key] as string | undefined }))
      .filter((f) => f.value);
  }, [sessionState]);

  // Pipeline state widgets — newest first
  const pipelineWidgets = useMemo(() => {
    return PIPELINE_STATE_KEYS.filter((p) => sessionState[p.key] != null);
  }, [sessionState]);

  const statusColor: Record<Status, string> = {
    running: "bg-blue-500",
    completed: "bg-green-500",
    error: "bg-destructive",
  };

  const showResultsToast =
    status === "completed" && appName !== "trend_trawler" && !toastDismissed;

  // Show right column if we have pipeline widgets or trend cards
  const hasRightColumn = pipelineWidgets.length > 0 || (status === "completed" && trends.length > 0);

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-8">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Agent Run</h1>
          <p className="mt-1 text-sm text-muted-foreground font-mono">
            {appName} / {sessionId}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {status === "completed" && appName !== "trend_trawler" && (
            <Button
              size="lg"
              onClick={() => router.push(resultsUrl)}
            >
              View Results
            </Button>
          )}
        </div>
      </div>

      {errorMsg && (
        <Card className="mb-4 border-destructive">
          <CardContent className="pt-4">
            <p className="text-sm text-destructive">{errorMsg}</p>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
        {/* Left sidebar — GCS output + campaign metadata */}
        <div className="space-y-3">
          {gcsUri && <GcsWidget uri={gcsUri} />}
          {researchReportUrl && (
            <Card className="shadow-sm animate-in fade-in slide-in-from-top-2 duration-300">
              <CardContent className="px-3 py-2.5">
                <dt className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Research Report
                </dt>
                <dd className="mt-1">
                  <a
                    href={researchReportUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 text-sm font-medium text-blue-600 hover:text-blue-800 hover:underline"
                  >
                    <svg className="h-4 w-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    View PDF Report
                  </a>
                </dd>
              </CardContent>
            </Card>
          )}
          <h2 className="text-sm font-semibold text-muted-foreground tracking-wider">
            Campaign Metadata
          </h2>
          {campaignFields.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">
              Waiting for metadata...
            </p>
          ) : (
            campaignFields.map((f) => (
              <Card key={f.key} className="shadow-sm">
                <CardContent className="px-3 py-2.5">
                  <dt className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                    {f.label}
                  </dt>
                  <dd className="mt-0.5 text-sm font-medium leading-snug">
                    {f.value}
                  </dd>
                </CardContent>
              </Card>
            ))
          )}
        </div>

        {/* Right content area */}
        <div>
          <div className={`grid gap-6 ${hasRightColumn ? "lg:grid-cols-[1fr_380px]" : ""}`}>
            {/* Event stream */}
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base flex items-center justify-between">
                  Event Stream
                  <span className="text-xs font-normal text-muted-foreground">
                    {events.length} event{events.length !== 1 ? "s" : ""}
                  </span>
                </CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <EventLog events={events} className="h-[600px]" />
              </CardContent>

              {/* Status bar under event stream */}
              <div className="border-t border-border px-4 py-3 flex items-center gap-2">
                <span
                  className={`inline-block h-2.5 w-2.5 rounded-full ${statusColor[status]} ${status === "running" ? "animate-pulse" : ""}`}
                />
                {status === "running" && (
                  <span className="text-sm text-muted-foreground flex items-center gap-1.5">
                    Agent is processing
                    <span className="processing-dots">
                      <span /><span /><span />
                    </span>
                  </span>
                )}
                {status === "completed" && (
                  <span className="text-sm text-green-600 font-medium">
                    Run completed
                  </span>
                )}
                {status === "error" && (
                  <span className="text-sm text-destructive font-medium">
                    Run failed
                  </span>
                )}
              </div>
            </Card>

            {/* Right column: pipeline widgets (newest first) or trend cards */}
            {hasRightColumn && (
              <div className="space-y-3 max-h-[700px] overflow-y-auto">
                {/* Trend cards — only for completed trend_trawler runs */}
                {status === "completed" && trends.length > 0 && (
                  <TrendCards trends={trends} campaignState={sessionState} />
                )}

                {/* Pipeline state widgets — newest at top */}
                {pipelineWidgets.map((p) => (
                  <PipelineWidget
                    key={p.key}
                    label={p.label}
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
          <Card className="shadow-lg border-green-200 bg-background">
            <CardContent className="flex items-center gap-4 px-5 py-4">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-green-100">
                <span className="inline-block h-3 w-3 rounded-full bg-green-500" />
              </div>
              <div className="mr-2">
                <p className="text-sm font-semibold">Run complete!</p>
                <p className="text-xs text-muted-foreground">
                  View results here
                </p>
              </div>
              <Button size="sm" onClick={() => router.push(resultsUrl)}>
                View Results
              </Button>
              <button
                onClick={() => setToastDismissed(true)}
                className="ml-1 text-muted-foreground hover:text-foreground text-lg leading-none"
                aria-label="Dismiss"
              >
                &times;
              </button>
            </CardContent>
          </Card>
        </div>
      )}

    </div>
  );
}
