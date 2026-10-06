"use client";

import { ExplainPanel } from "@/components/explain";
import { InfoTip } from "@/components/ui/info-tip";
import { formatPercent } from "@/lib/chart";
import { SHIFT_EXPLAIN } from "@/lib/experiment-explain";
import { SHIFT_HELP } from "@/lib/experiment-help";
import type { ShiftCard } from "@/lib/experiment-insights";
import { cn } from "@/lib/utils";
import { CONDENSED } from "@/app/results/[sessionId]/score-mark";

/**
 * One result card per scripted shift (Overview, under the headline): the
 * endpoint's best-creative rate just before → just after, how long it took to
 * recover, and the evidence behind it. No p-values; "Too early to call" under
 * five episodes.
 */
export function ShiftResults({
  cards,
  ghost,
  explain,
}: {
  cards: ShiftCard[];
  ghost: string;
  explain: boolean;
}) {
  if (!cards.length) return null;
  return (
    <section aria-labelledby="shift-results-heading" className="mt-8">
      <div className="flex items-center gap-1">
        <h3 id="shift-results-heading" className="text-base font-semibold text-foreground">
          How the endpoint handled your shifts
        </h3>
        <InfoTip label="About the shift results">{SHIFT_HELP.results}</InfoTip>
      </div>
      <ol className={cn("mt-3 grid gap-3", cards.length > 1 && "md:grid-cols-2")}>
        {cards.map((c) => {
          const early = c.status === "too_early" || c.status === "no_data";
          return (
            <li key={c.key} className="flex flex-col rounded-lg border border-border bg-card p-4">
              <p className="text-xs text-muted-foreground tabular-nums">
                {c.label}, {c.when.charAt(0).toLowerCase()}
                {c.when.slice(1)}
              </p>
              <p className="mt-0.5 text-sm font-semibold text-foreground">{c.title}</p>
              <div className="mt-3 flex items-end gap-3">
                <p
                  className={cn(
                    CONDENSED,
                    "text-[2.25rem] leading-none tabular-nums",
                    early ? "text-muted-foreground/60" : "text-foreground"
                  )}
                  aria-label={
                    c.before !== null && c.after !== null
                      ? `Best-creative rate ${formatPercent(c.before)} before, ${formatPercent(c.after)} after`
                      : undefined
                  }
                >
                  {c.before === null ? "–" : formatPercent(c.before)}
                  <span aria-hidden className="px-1.5 text-muted-foreground">
                    →
                  </span>
                  {c.after === null ? "–" : formatPercent(c.after)}
                </p>
                <p className="pb-1 text-xs leading-tight text-muted-foreground">
                  best-creative rate,
                  <br />
                  before and after
                </p>
              </div>
              {early && <p className="mt-3 text-sm font-semibold text-mark-pending">Too early to call</p>}
              <p className="mt-2 text-sm leading-snug text-foreground">{c.reading}</p>
              <p className="mt-auto pt-3 text-xs text-muted-foreground tabular-nums">{c.evidence}</p>
            </li>
          );
        })}
      </ol>
      {ghost && <p className="mt-3 max-w-[72ch] text-sm leading-snug text-foreground">{ghost}</p>}
      <ExplainPanel open={explain}>{`${SHIFT_EXPLAIN.cards} ${SHIFT_EXPLAIN.forgetting}`}</ExplainPanel>
    </section>
  );
}
