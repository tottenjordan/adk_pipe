"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** The "Explain" switch: reveals the "How to read this" panels across the page. */
export function ExplainSwitch({
  on,
  onChange,
  className,
}: {
  on: boolean;
  onChange: (on: boolean) => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={() => onChange(!on)}
      className={cn(
        "group inline-flex h-8 items-center gap-2 rounded-sm px-1.5 text-sm font-medium text-foreground outline-none select-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50",
        className
      )}
    >
      <span
        aria-hidden
        className="relative h-[18px] w-8 shrink-0 rounded-full bg-[#b9c2ca] transition-colors duration-200 group-aria-checked:bg-primary"
      >
        <span className="absolute top-[3px] left-[3px] size-3 rounded-full bg-white transition-transform duration-200 group-aria-checked:translate-x-3.5" />
      </span>
      Explain
    </button>
  );
}

/**
 * A "How to read this" block that opens with the Explain switch. Every panel on
 * the page opens together in one height-and-fade reveal (off under reduced
 * motion); while closed it is inert, so it is skipped by keyboard and screen readers.
 */
export function ExplainPanel({
  open,
  reading,
  children,
  className,
}: {
  open: boolean;
  /** The data-specific sentence, shown first in ink. */
  reading?: string | null;
  /** The general guidance, muted. */
  children?: ReactNode;
  className?: string;
}) {
  if (!reading && !children) return null;
  return (
    <div
      inert={!open}
      data-open={open || undefined}
      className={cn(
        "grid grid-rows-[0fr] opacity-0 transition-[grid-template-rows,opacity] duration-300 ease-out data-open:grid-rows-[1fr] data-open:opacity-100 motion-reduce:transition-none",
        className
      )}
    >
      <div className="min-h-0 overflow-hidden">
        <div className="mt-3 max-w-[72ch] rounded-sm bg-muted px-3 py-2.5 text-sm leading-relaxed">
          <p className="font-semibold text-foreground">How to read this</p>
          {reading && <p className="mt-1 text-foreground">{reading}</p>}
          {children && <p className="mt-1 text-muted-foreground">{children}</p>}
        </div>
      </div>
    </div>
  );
}
