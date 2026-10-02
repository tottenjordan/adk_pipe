"use client";

import { cn } from "@/lib/utils";

/**
 * A small inline group of mutually exclusive toggle buttons (the proof-grid
 * sort control pattern): ink fill for the active option, aria-pressed on each.
 */
export function SegmentedControl<T extends string>({
  label,
  labelledBy,
  options,
  value,
  onChange,
  disabled = false,
  className,
}: {
  /** Accessible group name (use `labelledBy` when a visible label exists). */
  label?: string;
  labelledBy?: string;
  options: readonly { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <div
      role="group"
      aria-label={labelledBy ? undefined : label}
      aria-labelledby={labelledBy}
      className={cn("inline-flex flex-wrap rounded-sm border border-border bg-card p-0.5", className)}
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={active}
            disabled={disabled}
            onClick={() => onChange(o.value)}
            className={cn(
              "h-7 rounded-sm px-2.5 text-xs font-medium transition-colors disabled:opacity-50",
              active
                ? "bg-foreground text-background"
                : "text-muted-foreground hover:bg-muted hover:text-foreground"
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
