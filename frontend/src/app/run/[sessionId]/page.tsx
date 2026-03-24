"use client";

import { useEffect, useState, useRef, useMemo, use } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EventLog } from "@/components/event-log";
import { TrendCards, parseTrendsMarkdown } from "@/components/trend-cards";
import { GcsWidget } from "@/components/gcs-widget";
import { streamRun } from "@/lib/api";
import type { AgentEvent } from "@/lib/types";

type Status = "running" | "completed" | "error";

/** Pipeline state keys to surface as collapsible widgets, in display order (newest first). */
const PIPELINE_STATE_KEYS = [
  { key: "final_visual_concepts", label: "Final Visual Concepts" },
  { key: "ad_copy_critique", label: "Ad Copy Critique" },
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

/** Color accent for specific field labels to make key fields pop. */
const FIELD_COLORS: Record<string, string> = {
  headline: "text-sky-400",
  body_text: "text-sky-300",
  concept_name: "text-purple-400",
  concept_summary: "text-purple-300",
  call_to_action: "text-amber-400",
  social_caption: "text-amber-300",
  trend_connection: "text-emerald-400",
  trend_visual_link: "text-emerald-400",
  trend: "text-emerald-300",
  trend_reference: "text-emerald-300",
  audience_appeal: "text-pink-400",
  audience_appeal_rationale: "text-pink-400",
  tone_style: "text-violet-400",
  image_generation_prompt: "text-cyan-400",
  critique_summary: "text-orange-400",
  selection_rationale: "text-orange-300",
  detailed_performance_rationale: "text-orange-300",
};

/** Assign a color to each pipeline widget by keyword. */
function widgetAccent(label: string): { dot: string; badge: string; text: string } {
  if (label.toLowerCase().includes("visual") && label.toLowerCase().includes("final"))
    return { dot: "bg-purple-400", badge: "bg-purple-400/15 text-purple-400", text: "text-purple-400" };
  if (label.toLowerCase().includes("critique"))
    return { dot: "bg-orange-400", badge: "bg-orange-400/15 text-orange-400", text: "text-orange-400" };
  if (label.toLowerCase().includes("visual"))
    return { dot: "bg-cyan-400", badge: "bg-cyan-400/15 text-cyan-400", text: "text-cyan-400" };
  if (label.toLowerCase().includes("ad copy"))
    return { dot: "bg-sky-400", badge: "bg-sky-400/15 text-sky-400", text: "text-sky-400" };
  return { dot: "bg-emerald-400", badge: "bg-emerald-400/15 text-emerald-400", text: "text-emerald-400" };
}

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
function ItemCard({
  item,
  index,
}: {
  item: Record<string, unknown>;
  index: number;
}) {
  const entries = Object.entries(item).filter(
    ([, v]) => v !== null && v !== undefined && v !== ""
  );
  const title =
    (item.concept_name as string) ||
    (item.headline as string) ||
    `Item ${index + 1}`;

  return (
    <div className="glass rounded-xl px-4 py-3 space-y-2.5">
      <div className="flex items-center gap-2 border-b border-white/8 pb-2.5">
        <Badge
          variant="secondary"
          className="text-[10px] px-1.5 py-0 bg-primary/15 text-primary border-0 font-bold"
        >
          {index + 1}
        </Badge>
        <span className="text-sm font-bold leading-tight text-foreground">
          {title}
        </span>
      </div>
      {entries.map(([key, value]) => {
        if (key === "concept_name" || key === "headline") return null;
        const label = FIELD_LABELS[key] || key.replace(/_/g, " ");
        const labelColor = FIELD_COLORS[key] || "text-muted-foreground";
        return (
          <div key={key}>
            <dt className={`text-[10px] font-bold uppercase tracking-wider ${labelColor}`}>
              {label}
            </dt>
            <dd className="text-xs leading-snug break-words whitespace-pre-wrap text-foreground/90 mt-0.5">
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
  const accent = widgetAccent(label);

  return (
    <>
      <div
        className="glass rounded-xl cursor-pointer transition-all duration-200
                   hover:border-primary/20 hover:shadow-lg hover:shadow-primary/5
                   animate-fadeInUpSmooth"
        onClick={() => setOpen(true)}
      >
        <div className="py-2.5 px-4 flex items-center justify-between">
          <span className="flex items-center gap-2 text-sm font-bold">
            <span className={`inline-block h-2 w-2 rounded-full ${accent.dot}`} />
            <span className={accent.text}>{label}</span>
            {itemCount > 0 && (
              <Badge
                variant="secondary"
                className={`text-[10px] px-1.5 py-0 border-0 font-bold ${accent.badge}`}
              >
                {itemCount}
              </Badge>
            )}
          </span>
          <span className="text-[10px] text-muted-foreground">
            click to view
          </span>
        </div>
      </div>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-md animate-in fade-in duration-150"
          onClick={() => setOpen(false)}
        >
          <div
            className="relative mx-4 w-full max-w-3xl max-h-[85vh] overflow-hidden flex flex-col
                       glass-strong rounded-2xl shadow-2xl shadow-black/60
                       animate-in zoom-in-95 duration-200"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-white/8 px-5 py-4">
              <h2 className="text-lg font-bold flex items-center gap-2">
                <span className={`inline-block h-2.5 w-2.5 rounded-full ${accent.dot}`} />
                <span className={accent.text}>{label}</span>
                {itemCount > 0 && (
                  <Badge
                    variant="secondary"
                    className={`border-0 font-bold ${accent.badge}`}
                  >
                    {itemCount} items
                  </Badge>
                )}
              </h2>
              <button
                onClick={() => setOpen(false)}
                className="flex h-7 w-7 items-center justify-center rounded-lg text-muted-foreground hover:text-foreground hover:bg-white/10 transition-colors"
                aria-label="Close"
              >
                &times;
              </button>
            </div>
            <div className="flex-1 overflow-auto p-5">
              {items ? (
                <div className="space-y-3">
                  {items.map((item, i) => (
                    <ItemCard key={i} item={item} index={i} />
                  ))}
                </div>
              ) : (
                <pre className="whitespace-pre-wrap break-words rounded-lg bg-white/5 p-4 text-sm font-mono leading-relaxed text-foreground/80">
                  {typeof data === "string"
                    ? data
                    : JSON.stringify(data, null, 2)}
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
  }, [
    sessionState.gcs_bucket,
    sessionState.gcs_folder,
    sessionState.agent_output_dir,
  ]);

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

  const statusConfig: Record<Status, { color: string; glow: string }> = {
    running: { color: "bg-blue-400", glow: "shadow-blue-400/30" },
    completed: { color: "bg-emerald-400", glow: "shadow-emerald-400/30" },
    error: { color: "bg-red-400", glow: "shadow-red-400/30" },
  };

  const showResultsToast =
    status === "completed" && appName !== "trend_trawler" && !toastDismissed;

  // Show right column if we have pipeline widgets or trend cards
  const hasRightColumn =
    pipelineWidgets.length > 0 ||
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
          {status === "completed" && appName !== "trend_trawler" && (
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
        <div className="mb-4 rounded-xl bg-red-400/10 border border-red-400/20 px-5 py-4 animate-fadeIn">
          <p className="text-sm text-red-400">{errorMsg}</p>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
        {/* Left sidebar — GCS output + campaign metadata */}
        <div className="space-y-3 animate-fadeInUp animation-delay-100 opacity-0" style={{ animationFillMode: "forwards" }}>
          {gcsUri && <GcsWidget uri={gcsUri} />}
          {researchReportUrl && (
            <div className="glass rounded-xl px-4 py-3 animate-fadeInUpSmooth">
              <dt className="text-[10px] font-bold uppercase tracking-wider text-amber-400">
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
                <dt className="text-[10px] font-bold uppercase tracking-wider text-sky-400">
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
              <div className="px-5 py-3 border-b border-white/5 flex items-center justify-between">
                <h3 className="text-sm font-semibold text-foreground">
                  Event Stream
                </h3>
                <span className="text-[10px] text-muted-foreground tabular-nums">
                  {events.length} event{events.length !== 1 ? "s" : ""}
                </span>
              </div>
              <EventLog events={events} className="h-[600px]" />

              {/* Status bar */}
              <div className="border-t border-white/5 px-5 py-3 flex items-center gap-2.5">
                <span
                  className={`inline-block h-2.5 w-2.5 rounded-full ${statusConfig[status].color} ${status === "running" ? "animate-pulse" : ""} shadow-lg ${statusConfig[status].glow}`}
                />
                {status === "running" && (
                  <span className="text-sm text-muted-foreground flex items-center gap-1.5">
                    Agent is processing
                    <span className="processing-dots text-blue-400">
                      <span />
                      <span />
                      <span />
                    </span>
                  </span>
                )}
                {status === "completed" && (
                  <span className="text-sm text-emerald-400 font-medium">
                    Run completed
                  </span>
                )}
                {status === "error" && (
                  <span className="text-sm text-red-400 font-medium">
                    Run failed
                  </span>
                )}
              </div>
            </div>

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
          <div className="glass-strong rounded-2xl shadow-2xl shadow-black/40 flex items-center gap-4 px-5 py-4">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-emerald-400/15">
              <span className="inline-block h-3 w-3 rounded-full bg-emerald-400 shadow-lg shadow-emerald-400/30" />
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
              className="ml-1 flex h-6 w-6 items-center justify-center rounded-lg text-muted-foreground hover:text-foreground hover:bg-white/10 transition-colors"
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
