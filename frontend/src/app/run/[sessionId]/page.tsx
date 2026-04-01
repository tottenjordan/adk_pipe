"use client";

import React, { useEffect, useState, useRef, useMemo, use } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EventLog } from "@/components/event-log";
import { TrendCards, parseTrendsMarkdown } from "@/components/trend-cards";
import { GcsWidget } from "@/components/gcs-widget";
import { Textarea } from "@/components/ui/textarea";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { streamRun, resumeRun, getSession } from "@/lib/api";
import type { AgentEvent } from "@/lib/types";

type Status = "running" | "completed" | "error" | "paused";

interface PauseContext {
  functionCallId: string;
  functionName: string;
  eventId: string;
}

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
  headline: "text-indigo-600",
  body_text: "text-indigo-500",
  concept_name: "text-violet-600",
  concept_summary: "text-violet-500",
  call_to_action: "text-amber-600",
  social_caption: "text-amber-500",
  trend_connection: "text-emerald-600",
  trend_visual_link: "text-emerald-600",
  trend: "text-emerald-600",
  trend_reference: "text-emerald-500",
  audience_appeal: "text-pink-600",
  audience_appeal_rationale: "text-pink-600",
  markets_product: "text-pink-500",
  tone_style: "text-violet-500",
  image_generation_prompt: "text-cyan-600",
  critique_summary: "text-orange-600",
  selection_rationale: "text-orange-500",
  detailed_performance_rationale: "text-orange-500",
};

/** Fields to hide from item cards. */
const HIDDEN_FIELDS = new Set(["id", "original_id", "ad_copy_id"]);

/** Per-widget layout config: side-by-side pairs + full-width field. */
const WIDGET_LAYOUTS: Record<string, { pairs: [string, string][]; fullWidth: string }> = {
  final_visual_concepts: {
    pairs: [
      ["trend", "trend_reference"],
      ["markets_product", "audience_appeal"],
      ["selection_rationale", "social_caption"],
      ["call_to_action", "concept_summary"],
    ],
    fullWidth: "image_generation_prompt",
  },
  ad_copy_critique: {
    pairs: [
      ["tone_style", "call_to_action"],
      ["trend_connection", "body_text"],
      ["audience_appeal_rationale", "social_caption"],
    ],
    fullWidth: "detailed_performance_rationale",
  },
};

/** Default layout for unknown widget types. */
const DEFAULT_LAYOUT = { pairs: [] as [string, string][], fullWidth: "" };

/** Assign a color to each pipeline widget by keyword. */
function widgetAccent(label: string): { dot: string; badge: string; text: string } {
  if (label.toLowerCase().includes("visual") && label.toLowerCase().includes("final"))
    return { dot: "bg-violet-500", badge: "bg-violet-50 text-violet-600 border-violet-200", text: "text-violet-600" };
  if (label.toLowerCase().includes("critique"))
    return { dot: "bg-orange-500", badge: "bg-orange-50 text-orange-600 border-orange-200", text: "text-orange-600" };
  if (label.toLowerCase().includes("visual"))
    return { dot: "bg-cyan-500", badge: "bg-cyan-50 text-cyan-600 border-cyan-200", text: "text-cyan-600" };
  if (label.toLowerCase().includes("ad copy"))
    return { dot: "bg-indigo-500", badge: "bg-indigo-50 text-indigo-600 border-indigo-200", text: "text-indigo-600" };
  return { dot: "bg-emerald-500", badge: "bg-emerald-50 text-emerald-600 border-emerald-200", text: "text-emerald-600" };
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

/** Render a single field with label + value. */
function FieldCell({ fieldKey, value }: { fieldKey: string; value: unknown }) {
  const label = FIELD_LABELS[fieldKey] || fieldKey.replace(/_/g, " ");
  const labelColor = FIELD_COLORS[fieldKey] || "text-muted-foreground";
  return (
    <div>
      <dt className={`text-[10px] font-bold uppercase tracking-wider ${labelColor}`}>
        {label}
      </dt>
      <dd className="text-xs leading-snug break-words whitespace-pre-wrap text-foreground/85 mt-0.5">
        {typeof value === "string" || typeof value === "number"
          ? String(value)
          : JSON.stringify(value, null, 2)}
      </dd>
    </div>
  );
}

/** Render a single item card with side-by-side panels layout. */
function ItemCard({
  item,
  index,
  widgetKey,
}: {
  item: Record<string, unknown>;
  index: number;
  widgetKey: string;
}): React.ReactNode {
  const title =
    (item.concept_name as string) ||
    (item.headline as string) ||
    `Item ${index + 1}`;

  const layout = WIDGET_LAYOUTS[widgetKey] || DEFAULT_LAYOUT;
  const pairs: [string, string][] = layout.pairs;
  const fullWidth = layout.fullWidth;

  // Collect fields used in structured layout
  const structuredFields = new Set<string>();
  pairs.forEach(([a, b]) => { structuredFields.add(a); structuredFields.add(b); });
  if (fullWidth) structuredFields.add(fullWidth);
  structuredFields.add("concept_name");
  structuredFields.add("headline");
  HIDDEN_FIELDS.forEach((f) => structuredFields.add(f));

  // Remaining fields not covered by structured layout
  const remainingEntries = Object.entries(item).filter(
    ([k, v]) => v !== null && v !== undefined && v !== "" && !structuredFields.has(k)
  );

  return (
    <div className="glass rounded-xl px-4 py-3 space-y-1.5">
      {/* Title bar */}
      <div className="flex items-center gap-2 border-b border-border pb-2">
        <Badge
          variant="secondary"
          className="text-[10px] px-1.5 py-0 bg-primary/10 text-primary border-0 font-bold"
        >
          {index + 1}
        </Badge>
        <span className="text-sm font-bold leading-tight text-foreground">
          {title}
        </span>
      </div>

      {/* Side-by-side panels */}
      {pairs.map(([leftKey, rightKey]): React.ReactNode => {
        const leftVal = item[leftKey];
        const rightVal = item[rightKey];
        if (!leftVal && !rightVal) return null;
        return (
          <div key={`${leftKey}-${rightKey}`} className="grid grid-cols-2 gap-2">
            <div className="rounded-md bg-muted/50 px-2.5 py-2">
              {leftVal ? (
                <FieldCell fieldKey={leftKey} value={leftVal} />
              ) : (
                <span className="text-xs text-muted-foreground italic">--</span>
              )}
            </div>
            <div className="rounded-md bg-muted/50 px-2.5 py-2">
              {rightVal ? (
                <FieldCell fieldKey={rightKey} value={rightVal} />
              ) : (
                <span className="text-xs text-muted-foreground italic">--</span>
              )}
            </div>
          </div>
        );
      })}

      {/* Full-width panel */}
      {fullWidth && !!item[fullWidth] && (
        <div className="rounded-md bg-cyan-50/60 border border-cyan-200/40 px-2.5 py-2">
          <FieldCell fieldKey={fullWidth} value={item[fullWidth]} />
        </div>
      )}

      {/* Remaining fields not in the structured layout */}
      {remainingEntries.length > 0 && (
        <div className="space-y-1.5 pt-1 border-t border-border">
          {remainingEntries.map(([key, value]) => (
            <FieldCell key={key} fieldKey={key} value={value} />
          ))}
        </div>
      )}
    </div>
  );
}

function PipelineWidget({
  label,
  stateKey,
  data,
}: {
  label: string;
  stateKey: string;
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
                   hover:shadow-md hover:shadow-black/5
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
                className={`text-[10px] px-1.5 py-0 border font-bold ${accent.badge}`}
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
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm animate-in fade-in duration-150"
          onClick={() => setOpen(false)}
        >
          <div
            className="relative mx-4 w-full max-w-3xl max-h-[85vh] overflow-hidden flex flex-col
                       glass-strong rounded-2xl shadow-2xl shadow-black/10
                       animate-in zoom-in-95 duration-200"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-border px-5 py-4">
              <h2 className="text-lg font-bold flex items-center gap-2">
                <span className={`inline-block h-2.5 w-2.5 rounded-full ${accent.dot}`} />
                <span className={accent.text}>{label}</span>
                {itemCount > 0 && (
                  <Badge
                    variant="secondary"
                    className={`border font-bold ${accent.badge}`}
                  >
                    {itemCount} items
                  </Badge>
                )}
              </h2>
              <button
                onClick={() => setOpen(false)}
                className="flex h-7 w-7 items-center justify-center rounded-lg text-muted-foreground hover:text-foreground hover:bg-black/5 transition-colors"
                aria-label="Close"
              >
                &times;
              </button>
            </div>
            <div className="flex-1 overflow-auto p-5">
              {items ? (
                <div className="space-y-3">
                  {items.map((item, i) => (
                    <ItemCard key={i} item={item} index={i} widgetKey={stateKey} />
                  ))}
                </div>
              ) : (
                <pre className="whitespace-pre-wrap break-words rounded-lg bg-muted/50 p-4 text-sm font-mono leading-relaxed text-foreground/80">
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

/* ── Review panel components for interactive mode ── */

function ReviewResearch({
  state,
  onResume,
}: {
  state: Record<string, unknown>;
  onResume: (response: Record<string, unknown>) => void;
}) {
  const [feedback, setFeedback] = useState("");
  const [editMode, setEditMode] = useState(false);
  const report = state.combined_final_cited_report as string | undefined;
  const [editedReport, setEditedReport] = useState(report ?? "");

  return (
    <div className="glass rounded-2xl p-6 space-y-4 animate-fadeInUp">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-amber-500" />
          <h2 className="text-lg font-semibold">Review Research Report</h2>
        </div>
        {report && (
          <button
            onClick={() => setEditMode((v) => !v)}
            className="text-xs font-medium text-primary hover:text-primary/80 transition-colors"
          >
            {editMode ? "Preview" : "Edit"}
          </button>
        )}
      </div>
      <p className="text-sm text-muted-foreground">
        Review the research findings below. Approve to continue to ad copy generation,
        or provide feedback for revisions.
      </p>
      {report && !editMode && (
        <div className="max-h-[28rem] overflow-y-auto rounded-lg bg-muted/30 p-5 border border-border prose prose-sm prose-neutral max-w-none
          prose-headings:text-foreground prose-headings:font-bold
          prose-h1:text-lg prose-h2:text-base prose-h3:text-sm
          prose-p:text-foreground/85 prose-p:leading-relaxed
          prose-a:text-primary prose-a:no-underline hover:prose-a:underline
          prose-strong:text-foreground prose-strong:font-semibold
          prose-li:text-foreground/85
          prose-code:text-xs prose-code:bg-muted prose-code:rounded prose-code:px-1
          prose-blockquote:border-l-primary/30 prose-blockquote:text-muted-foreground">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{report}</ReactMarkdown>
        </div>
      )}
      {report && editMode && (
        <Textarea
          value={editedReport}
          onChange={(e) => setEditedReport(e.target.value)}
          rows={16}
          className="bg-background border-border font-mono text-xs leading-relaxed max-h-[28rem]"
        />
      )}
      <Textarea
        placeholder="Optional feedback or revision requests..."
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={3}
        className="bg-background border-border"
      />
      <div className="flex gap-3">
        <Button onClick={() => onResume({ status: "approved", feedback, instruction: "User approved the research. Continue to the next step in the WORKFLOW." })}>
          Approve &amp; Continue
        </Button>
        <Button
          variant="outline"
          onClick={() => onResume({ status: "revision_requested", feedback, instruction: "User requested changes to the research. Address their feedback, then continue the WORKFLOW." })}
          disabled={!feedback}
        >
          Request Changes
        </Button>
      </div>
    </div>
  );
}

/** Labeled field for review cards */
function ReviewField({ label, value, color }: { label: string; value: string; color: string }) {
  if (!value) return null;
  return (
    <div>
      <dt className={`text-[10px] font-bold uppercase tracking-wider ${color}`}>{label}</dt>
      <dd className="mt-0.5 text-sm leading-snug text-foreground/85">{value}</dd>
    </div>
  );
}

function ReviewAdCopies({
  state,
  onResume,
}: {
  state: Record<string, unknown>;
  onResume: (response: Record<string, unknown>) => void;
}) {
  const [feedback, setFeedback] = useState("");
  const adCopies = extractItems(state.ad_copy_critique);

  return (
    <div className="glass rounded-2xl p-6 space-y-4 animate-fadeInUp">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-amber-500" />
        <h2 className="text-lg font-semibold">Review Ad Copies</h2>
      </div>
      <p className="text-sm text-muted-foreground">
        Review the generated ad copies. Approve to continue to visual concept generation.
      </p>
      {adCopies && (
        <div className="space-y-3 max-h-[28rem] overflow-y-auto">
          {adCopies.map((copy, i) => (
            <div key={i} className="rounded-xl border border-border/60 bg-background/50 p-4 space-y-3">
              {/* Title bar */}
              <div className="flex items-center gap-2 pb-2 border-b border-border/40">
                <Badge variant="secondary" className="text-[10px] px-1.5 py-0 bg-indigo-100 text-indigo-600 border-0 font-bold">
                  {i + 1}
                </Badge>
                <span className="text-sm font-bold text-foreground">
                  {String(copy.headline ?? `Ad Copy ${i + 1}`)}
                </span>
              </div>

              {/* Two-column field grid */}
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-md bg-muted/40 px-3 py-2">
                  <ReviewField label="Body Text" value={String(copy.body_text ?? "")} color="text-indigo-500" />
                </div>
                <div className="rounded-md bg-muted/40 px-3 py-2">
                  <ReviewField label="Tone / Style" value={String(copy.tone_style ?? "")} color="text-violet-500" />
                </div>
                <div className="rounded-md bg-muted/40 px-3 py-2">
                  <ReviewField label="Call to Action" value={String(copy.call_to_action ?? "")} color="text-amber-600" />
                </div>
                <div className="rounded-md bg-muted/40 px-3 py-2">
                  <ReviewField label="Trend Connection" value={String(copy.trend_connection ?? "")} color="text-emerald-600" />
                </div>
              </div>

              {/* Full-width fields */}
              {!!copy.audience_appeal_rationale && (
                <div className="rounded-md bg-pink-50/60 border border-pink-200/40 px-3 py-2">
                  <ReviewField label="Audience Appeal" value={String(copy.audience_appeal_rationale)} color="text-pink-600" />
                </div>
              )}
              {!!copy.social_caption && (
                <div className="rounded-md bg-amber-50/60 border border-amber-200/40 px-3 py-2">
                  <ReviewField label="Social Caption" value={String(copy.social_caption)} color="text-amber-500" />
                </div>
              )}
              {!!copy.detailed_performance_rationale && (
                <div className="rounded-md bg-orange-50/60 border border-orange-200/40 px-3 py-2">
                  <ReviewField label="Performance Rationale" value={String(copy.detailed_performance_rationale)} color="text-orange-500" />
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      <Textarea
        placeholder="Optional feedback..."
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={3}
        className="bg-background border-border"
      />
      <div className="flex gap-3">
        <Button onClick={() => onResume({ status: "approved", feedback, instruction: "User approved the ad copies. Continue to the next step in the WORKFLOW — generate visual concepts." })}>
          Approve &amp; Continue
        </Button>
      </div>
    </div>
  );
}

function ReviewVisualConcepts({
  state,
  onResume,
}: {
  state: Record<string, unknown>;
  onResume: (response: Record<string, unknown>) => void;
}) {
  const [feedback, setFeedback] = useState("");
  const concepts = extractItems(state.final_visual_concepts);

  return (
    <div className="glass rounded-2xl p-6 space-y-4 animate-fadeInUp">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-amber-500" />
        <h2 className="text-lg font-semibold">Review Visual Concepts</h2>
      </div>
      <p className="text-sm text-muted-foreground">
        Review the visual concepts and image prompts. Approve to generate images.
      </p>
      {concepts && (
        <div className="space-y-3 max-h-96 overflow-y-auto">
          {concepts.map((concept, i) => (
            <div key={i} className="rounded-lg border p-4 space-y-2">
              <div className="font-medium">{String(concept.concept_name ?? `Concept ${i + 1}`)}</div>
              <div className="text-sm text-muted-foreground">{String(concept.concept_summary ?? "")}</div>
              <div className="text-xs text-cyan-600 font-mono bg-muted/30 rounded p-2">
                {String(concept.image_generation_prompt ?? "")}
              </div>
            </div>
          ))}
        </div>
      )}
      <Textarea
        placeholder="Optional feedback..."
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={3}
        className="bg-background border-border"
      />
      <div className="flex gap-3">
        <Button onClick={() => onResume({ status: "approved", feedback, instruction: "User approved the visual concepts. Continue to the next step in the WORKFLOW — generate images." })}>
          Approve &amp; Generate Images
        </Button>
      </div>
    </div>
  );
}

function ReviewPanel({
  functionName,
  sessionState,
  onResume,
}: {
  functionName: string;
  sessionState: Record<string, unknown>;
  onResume: (response: Record<string, unknown>) => void;
}) {
  if (functionName === "review_research") {
    return <ReviewResearch state={sessionState} onResume={onResume} />;
  }
  if (functionName === "review_ad_copies") {
    return <ReviewAdCopies state={sessionState} onResume={onResume} />;
  }
  if (functionName === "review_visual_concepts") {
    return <ReviewVisualConcepts state={sessionState} onResume={onResume} />;
  }
  return null;
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
        let paused = false;
        for await (const event of streamRun(
          appName,
          userId,
          sessionId,
          message
        )) {
          // Deduplicate events by ID
          if (event.id && seenEventIds.current.has(event.id)) continue;
          if (event.id) seenEventIds.current.add(event.id);

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
              syncSessionState();
            }
          }
        }
        if (!paused) setStatus("completed");
      } catch (err) {
        setStatus("error");
        setErrorMsg(
          err instanceof Error ? err.message : "Agent run failed"
        );
      }
    }

    run();
  }, [appName, userId, sessionId]);

  // Resume from a paused long-running tool
  async function handleResume(response: Record<string, unknown>) {
    if (!pauseContext) return;
    setStatus("running");
    const ctx = pauseContext;
    setPauseContext(null);

    try {
      let paused = false;
      let eventCount = 0;
      for await (const event of resumeRun(
        appName,
        userId,
        sessionId,
        ctx.functionCallId,
        ctx.functionName,
        ctx.eventId,
        response
      )) {
        // Deduplicate events by ID
        if (event.id && seenEventIds.current.has(event.id)) continue;
        if (event.id) seenEventIds.current.add(event.id);
        eventCount++;

        setEvents((prev) => [...prev, event]);

        if (event.actions?.stateDelta) {
          setSessionState((prev) => ({
            ...prev,
            ...event.actions!.stateDelta,
          }));
        }

        // Check for another pause (skip partial/streaming events)
        if (!event.partial && event.longRunningToolIds && event.longRunningToolIds.length > 0) {
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
            return;
          }
        }
      }

      // If no events came back, the backend may not have resumed properly.
      // Fetch session state to check for active long-running tool calls.
      if (eventCount === 0) {
        console.warn("[resume] 0 events received — checking session for active pause");
        try {
          const session = await getSession(appName, userId, sessionId);
          if (session.state) {
            setSessionState((prev) => ({ ...prev, ...session.state }));
          }
          // Check if any event in the session has unresolved longRunningToolIds
          for (let i = session.events.length - 1; i >= 0; i--) {
            const evt = session.events[i];
            if (evt.longRunningToolIds && evt.longRunningToolIds.length > 0) {
              const functionCalls = evt.content?.parts
                ?.filter((p) => p.functionCall)
                ?.map((p) => p.functionCall!) ?? [];
              const pausedCall = functionCalls.find((fc) =>
                evt.longRunningToolIds!.includes(fc.id ?? "")
              );
              if (pausedCall) {
                // Check if this pause has already been responded to
                const hasResponse = session.events.slice(i + 1).some(
                  (e) => e.content?.parts?.some(
                    (p) => p.functionResponse?.id === pausedCall.id
                  )
                );
                if (!hasResponse) {
                  console.warn("[resume] Found unresolved pause:", pausedCall.name);
                  setPauseContext({
                    functionCallId: pausedCall.id ?? "",
                    functionName: pausedCall.name ?? "",
                    eventId: evt.id,
                  });
                  setStatus("paused");
                  paused = true;
                  break;
                }
              }
            }
          }
        } catch {
          // Session fetch failed, fall through to completed
        }
      }

      if (!paused) setStatus("completed");
    } catch (err) {
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : "Resume failed");
    }
  }

  // Fetch session state from backend to populate campaign metadata
  // that was set during a prior run/resume phase
  async function syncSessionState() {
    try {
      const session = await getSession(appName, userId, sessionId);
      if (session.state) {
        setSessionState((prev) => ({ ...prev, ...session.state }));
      }
    } catch {
      // Ignore — session may not exist yet
    }
  }

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
        value: (sessionState[f.key] ?? (f.altKey ? sessionState[f.altKey] : undefined)) as string | undefined,
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
  };

  const showResultsToast =
    status === "completed" && appName !== "trend_trawler" && !toastDismissed;

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

                {/* Trend cards — only for completed trend_trawler runs */}
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
