import { statusInfo, TONE_CLASSES } from "@/lib/experiments";
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
