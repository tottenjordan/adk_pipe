import { trendStalenessText } from "@/lib/trend-staleness";
import { cn } from "@/lib/utils";

/**
 * A calm amber notice when Google's trend data is stale (trend_scout state
 * `gtrends_staleness`). Renders nothing when the data is fresh or absent.
 */
export function TrendStalenessNotice({
  state,
  className,
}: {
  state: Record<string, unknown>;
  className?: string;
}) {
  const text = trendStalenessText(state);
  if (!text) return null;
  return (
    <p
      role="status"
      className={cn(
        "rounded-md border border-mark-pending/40 bg-mark-pending/5 px-3 py-2 text-sm text-foreground",
        className
      )}
    >
      {text}
    </p>
  );
}
