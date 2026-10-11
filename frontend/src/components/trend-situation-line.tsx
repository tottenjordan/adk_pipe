import { trendSituationText } from "@/lib/trend-situation";
import { cn } from "@/lib/utils";

/**
 * The trend's validated current moment as a muted, read-only line (run-page
 * outputs, results summary). Renders nothing when the run has no situation or
 * the feature is switched off (TREND_STATE_ENABLED=false).
 */
export function TrendSituationLine({
  state,
  className,
}: {
  state: Record<string, unknown>;
  className?: string;
}) {
  const text = trendSituationText(state);
  if (!text) return null;
  return (
    <p className={cn("text-sm text-muted-foreground", className)}>
      <span className="font-medium">Current moment:</span> {text}
    </p>
  );
}
