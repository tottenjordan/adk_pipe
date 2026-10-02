"use client";

import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EventLog } from "@/components/event-log";
import { FieldLabel } from "@/components/field-label";
import { cn, type DisplayField } from "@/lib/utils";
import type { AgentEvent } from "@/lib/types";
import { STAGE_DESCRIPTIONS, type RunStageStatus, type Stage } from "@/lib/run-stages";
import { ProcessingDots } from "./stage-spine";

/** Keys whose values make up the one-line brief, in order. */
const BRIEF_SUMMARY_KEYS = ["brand", "target_product", "target_search_trends"];

/**
 * One-line campaign brief (brand, product, trend) with a control that expands
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
        <span className="flex min-w-0 flex-1 items-baseline gap-x-4 truncate text-sm font-medium text-foreground">
          {summary.length > 0
            ? summary.map((part, i) => (
                <span key={i} className={i === 0 ? "shrink-0" : "min-w-0 truncate"}>
                  {part}
                </span>
              ))
            : "Campaign details"}
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
  if (status === "error") return "The run stopped before finishing. The error above says why.";
  if (status === "stalled") {
    return "No activity for a while. It may still be running in the background — reload to reconnect.";
  }
  if (!stage) return "Starting the run.";
  return STAGE_DESCRIPTIONS[stage.id] ?? "The agent is working.";
}

/** A run whose segment ended before the workflow finished, and how to continue it. */
export interface StoppedEarlyProps {
  /** Label of the first stage that didn't finish. */
  stage: string;
  onContinue: () => void;
  /** The continue request is in flight. */
  continuing: boolean;
  /** Why the last continue attempt failed, or null. */
  error: string | null;
}

/** The current-stage panel for a run that stopped early: explanation + "Continue run". */
function StoppedEarlyPanel({
  stopped,
  lastUpdate,
}: {
  stopped: StoppedEarlyProps;
  lastUpdate: string;
}) {
  return (
    <section aria-live="polite" className="rounded-lg border border-border bg-card px-5 py-4">
      <h2 className="text-lg font-semibold text-foreground">Stopped before {stopped.stage}</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        The run stopped before {stopped.stage}. This sometimes happens when the model returns an
        empty response. Continue the run to pick up where it left off.
      </p>
      {stopped.error && (
        <p role="alert" className="mt-3 text-sm text-mark-fail">
          Couldn&apos;t continue the run: {stopped.error}. Try again, or start a new run.
        </p>
      )}
      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2">
        <Button onClick={stopped.onContinue} disabled={stopped.continuing}>
          {stopped.continuing ? "Continuing…" : "Continue run"}
        </Button>
        {lastUpdate && (
          <span className="text-xs text-muted-foreground tabular-nums">
            Last update {lastUpdate}
          </span>
        )}
      </div>
    </section>
  );
}

/** The main-area panel describing what the run is doing right now. */
export function CurrentStagePanel({
  stage,
  stages,
  status,
  lastUpdate,
  stopped = null,
}: {
  stage: Stage | null;
  stages: Stage[];
  status: RunStageStatus;
  /** Formatted local time of the latest event, or "". */
  lastUpdate: string;
  /** Set when the run stopped early (replaces the "Run complete" panel). */
  stopped?: StoppedEarlyProps | null;
}) {
  if (stopped) return <StoppedEarlyPanel stopped={stopped} lastUpdate={lastUpdate} />;
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
            <p className="mt-2 flex flex-wrap gap-x-4 text-xs text-muted-foreground tabular-nums">
              {meta.map((m) => (
                <span key={m}>{m}</span>
              ))}
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
