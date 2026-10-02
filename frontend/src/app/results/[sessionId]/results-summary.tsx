import Link from "next/link";
import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { dimensionLabel } from "@/lib/eval-dimensions";
import { passThreshold, type EvalReport } from "@/lib/eval-matching";
import { summarySentence } from "@/lib/results-copy";
import { pct } from "./score-mark";

export type EvalStatus = "idle" | "loading" | "pending" | "error";

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
 * degraded research, and the eval-report-still-writing refresh state.
 */
export function ResultsSummary({
  report,
  status,
  onRefresh,
  refreshDisabled,
  noImages,
  degradationWarnings,
}: {
  report: EvalReport | null;
  status: EvalStatus;
  onRefresh: () => void;
  refreshDisabled: boolean;
  noImages: boolean;
  degradationWarnings: string[];
}) {
  const threshold = passThreshold(report);
  const weakest = report?.summary.weakest_dimensions ?? [];
  const overall = report?.summary.overall_pass_rate ?? 0;

  return (
    <>
      {/* Zero-image warning: the image producer exhausted all retries (issue #116). */}
      {noImages && (
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

      {/* Degraded research: some producers exhausted retries (filtered against
          the zero-image banner so the same note never shows twice). */}
      {degradationWarnings.length > 0 && (
        <Notice title="Some research steps produced no output">
          <p>The creative was built on incomplete research. Check claims against the brief before using it.</p>
          <ul className="mt-1.5 list-disc space-y-0.5 pl-4">
            {degradationWarnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
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
