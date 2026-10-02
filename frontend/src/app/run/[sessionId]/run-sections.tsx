"use client";

import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { EventLog } from "@/components/event-log";
import { FieldLabel } from "@/components/field-label";
import { cn, type DisplayField } from "@/lib/utils";
import type { AgentEvent } from "@/lib/types";
import { STAGE_DESCRIPTIONS, type RunStageStatus, type Stage } from "@/lib/run-stages";
import { ProcessingDots } from "./stage-spine";

/** Keys whose values make up the one-line brief, in order. */
const BRIEF_SUMMARY_KEYS = ["brand", "target_product", "target_search_trends"];

/**
 * One-line campaign brief (brand · product · trend) with a control that expands
 * the full campaign fields and any visual direction.
 */
export function BriefSummary({
  campaignFields,
  visualDirectionFields,
}: {
  campaignFields: DisplayField[];
  visualDirectionFields: DisplayField[];
}) {
  const [open, setOpen] = useState(false);
  const summary = BRIEF_SUMMARY_KEYS.map(
    (k) => campaignFields.find((f) => f.key === k)?.value
  ).filter(Boolean);

  if (campaignFields.length === 0 && visualDirectionFields.length === 0) {
    return <p className="text-sm text-muted-foreground">Waiting for the brief…</p>;
  }

  return (
    <div className="rounded-lg border border-border bg-card">
      <button
        type="button"
        aria-expanded={open}
        aria-controls="run-brief-details"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 rounded-lg px-4 py-2.5 text-left"
      >
        <span className="shrink-0 text-xs font-medium text-muted-foreground">Brief</span>
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
          {summary.length > 0 ? summary.join(" · ") : "Campaign details"}
        </span>
        <span className="flex shrink-0 items-center gap-1 text-xs font-medium text-primary">
          {open ? "Hide" : "Show all"}
          <ChevronDown
            aria-hidden
            className={cn("h-3.5 w-3.5", open && "rotate-180")}
          />
        </span>
      </button>
      {open && (
        <div id="run-brief-details" className="border-t border-border px-4 py-3">
          <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
            {campaignFields.map((f) => (
              <div key={f.key}>
                <FieldLabel as="dt">{f.label}</FieldLabel>
                <dd className="mt-0.5 text-sm leading-snug text-foreground break-words">
                  {f.value}
                </dd>
              </div>
            ))}
          </dl>
          {visualDirectionFields.length > 0 && (
            <>
              <h3 className="mt-4 text-sm font-semibold text-foreground">Visual direction</h3>
              <dl className="mt-2 grid gap-x-6 gap-y-3 sm:grid-cols-2">
                {visualDirectionFields.map((f) => (
                  <div key={f.key}>
                    <FieldLabel as="dt">{f.label}</FieldLabel>
                    <dd className="mt-0.5 text-sm leading-snug text-foreground break-words">
                      {f.value}
                    </dd>
                  </div>
                ))}
              </dl>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** Plain description for the current-stage panel. */
function describe(stage: Stage | null, status: RunStageStatus, stages: Stage[]): string {
  if (status === "completed") {
    return stages.every((s) => s.state === "done")
      ? "Every stage finished."
      : "The run finished. Stages marked not started produced no output.";
  }
  if (status === "error") return "The run stopped. See the error above.";
  if (status === "stalled") {
    return "No activity for a while. It may still be running in the background — reload to reconnect.";
  }
  if (!stage) return "Starting the run.";
  return STAGE_DESCRIPTIONS[stage.id] ?? "The agent is working.";
}

/** The main-area panel describing what the run is doing right now. */
export function CurrentStagePanel({
  stage,
  stages,
  status,
  lastUpdate,
}: {
  stage: Stage | null;
  stages: Stage[];
  status: RunStageStatus;
  /** Formatted local time of the latest event, or "". */
  lastUpdate: string;
}) {
  const index = stage ? stages.findIndex((s) => s.id === stage.id) : -1;
  const heading =
    status === "completed" ? "Run complete" : stage ? stage.label : "Starting";
  const meta = [
    status !== "completed" && index >= 0 ? `Step ${index + 1} of ${stages.length}` : "",
    lastUpdate ? `Last update ${lastUpdate}` : "",
  ].filter(Boolean);

  return (
    <section
      aria-live="polite"
      className="rounded-lg border border-border bg-card px-5 py-4"
    >
      <div>
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-lg font-semibold text-foreground">
            {heading}
            {status === "running" && <ProcessingDots />}
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {describe(stage, status, stages)}
          </p>
          {meta.length > 0 && (
            <p className="mt-2 text-xs text-muted-foreground tabular-nums">
              {meta.join(" · ")}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

/**
 * The raw event stream, tucked into a disclosure. The log only mounts while
 * open, so a long run doesn't pay to render hundreds of hidden events.
 */
export function TechnicalLog({ events }: { events: AgentEvent[] }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="group rounded-lg border border-border bg-card"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-lg px-5 py-3 [&::-webkit-details-marker]:hidden">
        <span className="flex items-center gap-2 text-sm font-semibold text-foreground">
          <ChevronDown aria-hidden className="h-4 w-4 -rotate-90 group-open:rotate-0" />
          Technical log
        </span>
        <span className="text-xs text-muted-foreground tabular-nums">
          {events.length} event{events.length !== 1 ? "s" : ""}
        </span>
      </summary>
      {open && (
        <div className="border-t border-border">
          <EventLog events={events} className="h-[520px]" />
        </div>
      )}
    </details>
  );
}
