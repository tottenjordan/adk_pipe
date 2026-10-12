import { cn } from "@/lib/utils";

/** The note shown when a sensitive trend is picked or prefilled. */
export const SENSITIVE_TREND_ADVICE =
  "Creatives will never reference the event, so they may connect only loosely; many brands avoid trends like this.";

/** "Sensitive event (reason)" sentence for a picked trend. */
export function sensitiveEventNote(reason: string): string {
  const why = reason ? ` (${reason})` : "";
  return `This trend is about a sensitive event${why}. ${SENSITIVE_TREND_ADVICE}`;
}

/**
 * Amber "Sensitive" chip for a trend trend_scout flagged as a sensitive
 * event. The reason is the hover title; pair it with a described-by element
 * for screen readers (title alone is not reliably announced).
 */
export function SensitiveChip({ reason, className }: { reason?: string; className?: string }) {
  return (
    <span
      title={reason || undefined}
      className={cn(
        "inline-flex shrink-0 items-center rounded-sm border border-mark-pending px-1.5 py-0.5 text-xs font-medium leading-none text-mark-pending",
        className
      )}
    >
      Sensitive
    </span>
  );
}
