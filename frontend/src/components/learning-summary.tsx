import { learningSummaryText } from "@/lib/rating-learning";
import { cn } from "@/lib/utils";

/**
 * What opt-in rating-driven learning applied to this run, as a quiet card
 * (run-page outputs, results summary). Renders nothing when the run didn't
 * opt in, the learning step hasn't recorded anything yet, or no effect was
 * enabled. The compact on/off status stays in the campaign metadata row
 * ("Learn from ratings").
 */
export function LearningSummary({
  state,
  className,
}: {
  state: Record<string, unknown>;
  className?: string;
}) {
  const text = learningSummaryText(state);
  if (!text) return null;
  return (
    <div className={cn("rounded-lg border border-border bg-card px-4 py-3", className)}>
      <span className="block text-xs font-medium text-muted-foreground">Learning from ratings</span>
      <p className="mt-0.5 text-sm text-foreground">{text}</p>
    </div>
  );
}
