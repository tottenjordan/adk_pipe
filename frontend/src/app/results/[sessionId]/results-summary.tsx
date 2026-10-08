import Link from "next/link";
import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { dimensionLabel } from "@/lib/eval-dimensions";
import { passThreshold, type EvalReport } from "@/lib/eval-matching";
import { summarySentence } from "@/lib/results-copy";
import type { ClassifiedWarnings } from "@/lib/run-warnings";
import { pct } from "./score-mark";

export type EvalStatus = "idle" | "loading" | "pending" | "error";

function NoteList({ notes }: { notes: string[] }) {
  return (
    <ul className="mt-1.5 list-disc space-y-0.5 pl-4">
      {notes.map((w) => (
        <li key={w}>{w}</li>
      ))}
    </ul>
  );
}

function Notice({
  title,
  children,
  action,
}: {
  title: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div
      role="status"
      className="mb-4 flex items-start justify-between gap-4 rounded-lg border border-border border-l-4 border-l-mark-pending bg-card px-4 py-3"
    >
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
        {children && <div className="mt-0.5 text-xs leading-snug text-foreground/85">{children}</div>}
      </div>
      {action}
    </div>
  );
}

/**
 * Plain-sentence evaluation summary plus the run's warnings: zero images,
 * degraded research, failed saves, quality-check flags, and the
 * eval-report-still-writing refresh state.
 */
export function ResultsSummary({
  report,
  status,
  onRefresh,
  refreshDisabled,
  noImages,
  runWarnings,
  stoppedBefore = null,
  runUrl,
}: {
  report: EvalReport | null;
  status: EvalStatus;
  onRefresh: () => void;
  refreshDisabled: boolean;
  noImages: boolean;
  /** The eval report's run notes, grouped by {@link classifyWarnings}. */
  runWarnings: ClassifiedWarnings;
  /** Label of the stage the run stopped before, when it stopped early. */
  stoppedBefore?: string | null;
  /** The run page, where a stopped run can be continued. */
  runUrl?: string;
}) {
  const threshold = passThreshold(report);
  const weakest = report?.summary.weakest_dimensions ?? [];
  const overall = report?.summary.overall_pass_rate ?? 0;

  return (
    <>
      {/* Stopped early: the run ended before its last stage. Takes precedence
          over the zero-image banner (continuing the run is the fix for both). */}
      {stoppedBefore && (
        <Notice
          title={`This run stopped before ${stoppedBefore}, so some outputs are missing.`}
          action={
            runUrl ? (
              <Link
                href={runUrl}
                className={buttonVariants({
                  variant: "outline",
                  size: "sm",
                  className: "shrink-0",
                })}
              >
                Open run
              </Link>
            ) : undefined
          }
        >
          Open the run to continue it from where it left off.
        </Notice>
      )}

      {/* Zero-image warning: the image producer exhausted all retries (issue #116). */}
      {noImages && !stoppedBefore && (
        <Notice
          title="No images were generated for this run"
          action={
            <Link
              href="/"
              className={buttonVariants({ variant: "outline", size: "sm", className: "shrink-0" })}
            >
              Start a new run
            </Link>
          }
        >
          The image model failed on every retry (MALFORMED_FUNCTION_CALL), so this
          run has copy but no visuals. This is usually transient: run the same
          campaign again to get images.
        </Notice>
      )}

      {/* Degraded research: a research/brief producer exhausted retries (the
          image exhaustion is grouped out — it has the zero-image banner). */}
      {runWarnings.research.length > 0 && (
        <Notice title="Some research steps produced no output">
          <p>The creative was built on incomplete research. Check claims against the brief before using it.</p>
          <NoteList notes={runWarnings.research} />
        </Notice>
      )}

      {/* Persistence failures from the finalize step (PDF, gallery, BigQuery rows). */}
      {runWarnings.saves.length > 0 && (
        <Notice title="Some outputs could not be saved">
          <NoteList notes={runWarnings.saves} />
        </Notice>
      )}

      {/* Residual quality-check flags and judge notes: informational, not a failure. */}
      {runWarnings.quality.length > 0 && (
        <Notice title="Quality checks flagged issues">
          <p>Automated checks flagged these items; review them before using the creatives.</p>
          <NoteList notes={runWarnings.quality} />
        </Notice>
      )}

      {/* Eval report still being written (it's the run's last artifact) or failed. */}
      {!report && status !== "idle" && (
        <div className="mb-4 flex items-center justify-between gap-4 rounded-lg border border-border bg-card px-4 py-3">
          <p className="text-sm text-foreground">
            {status === "loading" && "Loading the evaluation report…"}
            {status === "pending" &&
              "The evaluation report is still being written. It's the last step of the run, so refresh in a minute."}
            {status === "error" && "The evaluation report didn't load. Refresh to try again."}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="shrink-0"
            disabled={status === "loading" || refreshDisabled}
            onClick={onRefresh}
          >
            {status === "loading" ? "Loading…" : "Refresh"}
          </Button>
        </div>
      )}

      {report && (
        <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 rounded-lg border border-border bg-card px-4 py-3">
          <p className="text-base text-foreground">
            <span className="font-semibold tabular-nums">{summarySentence(report.summary)}</span>
            {weakest.length > 0 && (
              <span className="text-muted-foreground">
                {" "}Weakest: {weakest.map(dimensionLabel).join(", ")}.
              </span>
            )}
          </p>
          <p className="text-xs text-muted-foreground tabular-nums">
            Overall pass rate{" "}
            <span
              className={cn(
                "font-semibold",
                overall >= threshold ? "text-mark-pass" : "text-mark-fail"
              )}
            >
              {pct(overall)}%
            </span>
            , pass mark {pct(threshold)}%
          </p>
        </div>
      )}
    </>
  );
}
