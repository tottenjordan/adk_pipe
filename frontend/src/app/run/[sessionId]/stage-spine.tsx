"use client";

import { Check } from "lucide-react";
import { cn } from "@/lib/utils";
import type { RunStageStatus, Stage, StageState } from "@/lib/run-stages";

/** The bouncing dots — the only motion on the run page. */
export function ProcessingDots({ className }: { className?: string }) {
  return (
    <span aria-hidden className={cn("processing-dots text-primary", className)}>
      <span />
      <span />
      <span />
    </span>
  );
}

/** Plain-words state for a stage, given the run's coarse status. */
export function stageStateText(state: StageState, runStatus: RunStageStatus): string {
  switch (state) {
    case "done":
      return "Done";
    case "needs_review":
      return "Needs your review";
    case "degraded":
      return "Finished with gaps";
    case "active":
      if (runStatus === "error") return "Stopped here";
      if (runStatus === "stalled") return "No recent activity";
      return "In progress";
    default:
      return "Not started";
  }
}

function Marker({ stage, index }: { stage: Stage; index: number }) {
  const base =
    "flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xs font-semibold tabular-nums lg:h-7 lg:w-7";
  switch (stage.state) {
    case "done":
      return (
        <span className={cn(base, "border-mark-pass bg-mark-pass/10 text-mark-pass")}>
          <Check className="h-3.5 w-3.5" strokeWidth={3} aria-hidden />
        </span>
      );
    case "active":
      return (
        <span className={cn(base, "border-primary bg-primary text-primary-foreground")}>
          {index + 1}
        </span>
      );
    case "needs_review":
      return (
        <span className={cn(base, "border-mark-pending bg-mark-pending/15 text-mark-pending")}>
          {index + 1}
        </span>
      );
    case "degraded":
      return (
        <span className={cn(base, "border-mark-fail bg-mark-fail/10 text-mark-fail")}>
          !
        </span>
      );
    default:
      return (
        <span className={cn(base, "border-border bg-card text-muted-foreground")}>
          {index + 1}
        </span>
      );
  }
}

const STATE_TEXT_CLASS: Record<StageState, string> = {
  done: "text-mark-pass",
  active: "text-primary",
  needs_review: "text-mark-pending",
  degraded: "text-mark-fail",
  pending: "text-muted-foreground",
};

/**
 * Numbered stage spine. A vertical list from `lg` up; below that a compact
 * horizontal step bar (markers only) with the current step spelled out under it.
 */
export function StageSpine({
  stages,
  current,
  runStatus,
}: {
  stages: Stage[];
  current: Stage | null;
  runStatus: RunStageStatus;
}) {
  const currentIndex = current ? stages.findIndex((s) => s.id === current.id) : -1;
  return (
    <nav aria-label="Run stages">
      <ol className="flex items-center gap-1 overflow-x-auto pb-1 lg:flex-col lg:items-stretch lg:gap-0 lg:overflow-visible lg:pb-0">
        {stages.map((stage, i) => {
          const isCurrent = i === currentIndex;
          const isLast = i === stages.length - 1;
          return (
            <li
              key={stage.id}
              aria-current={isCurrent ? "step" : undefined}
              className="flex shrink-0 items-center gap-1 lg:relative lg:items-start lg:gap-3 lg:pb-5 lg:last:pb-0"
            >
              {/* Connector to the next stage (vertical on lg, horizontal below). */}
              {!isLast && (
                <span
                  aria-hidden
                  className={cn(
                    "hidden lg:block lg:absolute lg:left-[13px] lg:top-8 lg:bottom-1 lg:w-px",
                    stage.state === "done" || stage.state === "degraded"
                      ? "bg-mark-pass/40"
                      : "bg-border"
                  )}
                />
              )}
              <Marker stage={stage} index={i} />
              <span className="sr-only lg:not-sr-only lg:min-w-0 lg:pt-0.5">
                <span
                  className={cn(
                    "block text-sm leading-snug",
                    stage.state === "pending" ? "text-muted-foreground" : "font-medium text-foreground"
                  )}
                >
                  {stage.label}
                </span>
                <span
                  className={cn(
                    "flex items-center gap-1.5 text-xs leading-snug",
                    STATE_TEXT_CLASS[stage.state]
                  )}
                >
                  {stage.state === "pending" ? (
                    <span className="sr-only">Not started</span>
                  ) : (
                    stageStateText(stage.state, runStatus)
                  )}
                  {stage.state === "active" && runStatus === "running" && <ProcessingDots />}
                </span>
              </span>
              {!isLast && (
                <span aria-hidden className="h-px w-2 shrink-0 bg-border lg:hidden" />
              )}
            </li>
          );
        })}
      </ol>
      {current && (
        <p className="mt-2 text-sm text-muted-foreground lg:hidden" aria-hidden>
          Step {currentIndex + 1} of {stages.length}:{" "}
          <span className="font-medium text-foreground">{current.label}</span>
          {", "}
          <span className={STATE_TEXT_CLASS[current.state]}>
            {stageStateText(current.state, runStatus)}
          </span>
        </p>
      )}
    </nav>
  );
}
