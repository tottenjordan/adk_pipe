import { STATUS_HELP } from "@/lib/experiment-help";
import { statusInfo, TONE_CLASSES, type ExperimentStatus } from "@/lib/experiments";
import { cn } from "@/lib/utils";

/** Status dot + sentence-case label (the word carries the meaning, not the colour). */
export function ExperimentStatusLabel({ status, className }: { status: string; className?: string }) {
  const { label, tone } = statusInfo(status);
  const c = TONE_CLASSES[tone];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-xs font-medium whitespace-nowrap",
        c.text,
        className
      )}
    >
      <span aria-hidden className={cn("size-2 shrink-0 rounded-full", c.dot)} />
      {label}
    </span>
  );
}

/** One line per status, for the status "ⓘ" popover; the current status is emphasised. */
export function StatusHelp({ current }: { current?: string }) {
  return (
    <ul className="space-y-1">
      {(Object.keys(STATUS_HELP) as ExperimentStatus[]).map((s) => (
        <li key={s} className={cn(s === current ? "text-foreground" : "text-muted-foreground")}>
          <span className="font-medium text-foreground">{statusInfo(s).label}:</span> {STATUS_HELP[s]}
          {s === current && <span className="sr-only"> (current status)</span>}
        </li>
      ))}
    </ul>
  );
}
