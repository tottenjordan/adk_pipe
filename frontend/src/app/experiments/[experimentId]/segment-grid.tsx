"use client";

import { ExplainPanel } from "@/components/explain";
import { formatInt, formatPercent } from "@/lib/chart";
import { cellShade, type SegmentGrid } from "@/lib/creative-detail";
import { CREATIVE_DETAIL_EXPLAIN } from "@/lib/experiment-explain";
import { segmentLabel } from "@/lib/experiments";
import type { Lane } from "@/lib/scoreboard";
import { cn } from "@/lib/utils";
import { CONDENSED } from "@/app/results/[sessionId]/score-mark";

/** Ink alpha at the lowest and (MIN + SPAN) at the highest click rate: light enough for ink text. */
const SHADE_MIN = 0.03;
const SHADE_SPAN = 0.22;

/**
 * Creative × segment click-rate grid (Overview, below the scoreboard): one
 * neutral shading scale for the whole grid, the best creative per segment
 * outlined. Creative names open the detail drawer.
 */
export function SegmentGridView({
  grid,
  lanes,
  explain,
  onOpen,
}: {
  grid: SegmentGrid;
  lanes: Lane[];
  explain: boolean;
  onOpen: (creativeId: string) => void;
}) {
  const byId = new Map(lanes.map((l) => [l.creativeId, l]));
  const cols = `minmax(9rem,1.2fr) repeat(${grid.segments.length}, minmax(5.5rem,1fr))`;
  return (
    <section aria-labelledby="segment-grid-heading" className="mt-10">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        <h2 id="segment-grid-heading" className="text-base font-semibold text-foreground">
          Click rate by audience segment
        </h2>
        {grid.range && (
          <p className="flex items-center gap-2 text-xs text-muted-foreground tabular-nums">
            {formatPercent(grid.range[0], 1)}
            <span
              aria-hidden
              className="h-2.5 w-20 rounded-[2px]"
              style={{
                backgroundImage: `linear-gradient(to right, rgb(26 29 33 / ${SHADE_MIN}), rgb(26 29 33 / ${SHADE_MIN + SHADE_SPAN}))`,
              }}
            />
            {formatPercent(grid.range[1], 1)}
            <span className="ml-4">Outlined in its colour: best creative for the segment</span>
          </p>
        )}
      </div>
      <ExplainPanel open={explain}>{CREATIVE_DETAIL_EXPLAIN.grid}</ExplainPanel>

      <div className="mt-3 overflow-x-auto rounded-lg border border-border bg-card">
        <table className="w-full min-w-[34rem] border-collapse text-sm">
          <caption className="sr-only">
            Click rate of each creative with each audience segment; the best creative per segment is marked.
          </caption>
          <thead>
            <tr className="grid border-b border-border" style={{ gridTemplateColumns: cols }}>
              <th scope="col" className="px-4 py-2 text-left text-xs font-normal text-muted-foreground">
                Creative
              </th>
              {grid.segments.map((s) => (
                <th key={s} scope="col" className="px-2 py-2 text-center text-xs font-normal text-muted-foreground">
                  {segmentLabel(s)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.rows.map((row) => {
              const lane = byId.get(row.creativeId);
              const name = lane?.name ?? row.creativeId;
              return (
                <tr
                  key={row.creativeId}
                  className="grid border-b border-border last:border-b-0"
                  style={{ gridTemplateColumns: cols }}
                >
                  <th scope="row" className="flex min-w-0 items-center px-4 py-1.5 text-left font-semibold">
                    <button
                      type="button"
                      onClick={() => onOpen(row.creativeId)}
                      className="flex min-w-0 items-center gap-2 rounded-sm text-left text-foreground underline-offset-4 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"
                    >
                      <span aria-hidden className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: lane?.color }} />
                      <span className="truncate">{name}</span>
                    </button>
                  </th>
                  {row.cells.map((c) => {
                    const shade = cellShade(c.ctr, grid.range);
                    return (
                      <td key={c.segment} className="p-1">
                        <div
                          title={c.ctr === null ? undefined : `${formatInt(c.impressions)} impressions`}
                          className="relative flex h-14 items-center justify-center rounded-[3px] text-foreground tabular-nums"
                          style={{
                            backgroundColor: `rgb(26 29 33 / ${(SHADE_MIN + shade * SHADE_SPAN).toFixed(3)})`,
                            boxShadow: c.isBest && lane ? `inset 0 0 0 2px ${lane.color}` : undefined,
                          }}
                        >
                          <span className={cn(CONDENSED, "text-[1.375rem] leading-none", c.ctr === null && "text-muted-foreground/50")}>
                            {c.ctr === null ? "–" : formatPercent(c.ctr, 1)}
                          </span>
                          {c.isBest && (
                            <span
                              className="absolute top-1 right-1.5 text-[11px] leading-none font-semibold"
                              style={{ color: lane?.color }}
                            >
                              Best
                              <span className="sr-only"> for this segment</span>
                            </span>
                          )}
                        </div>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
