"use client";

import { useId } from "react";
import { InfoTip } from "@/components/ui/info-tip";
import { SHIFT_HELP } from "@/lib/experiment-help";
import type { TrafficRun } from "@/lib/experiments";
import { runLabel } from "@/lib/shifts";

/**
 * Which numbered traffic run the results show (`?run=N`; contracts §10). With a
 * single run it is a plain label; with several, a select (newest by default).
 */
export function RunSelector({
  runs,
  selected,
  onSelect,
}: {
  runs: TrafficRun[];
  selected: TrafficRun;
  onSelect: (run: number | null) => void;
}) {
  const id = useId();
  const total = runs.length;
  return (
    <div className="flex items-center gap-1.5">
      {total > 1 ? (
        <>
          <label htmlFor={id} className="sr-only">
            Traffic run
          </label>
          <select
            id={id}
            value={selected.run}
            onChange={(e) => {
              const n = Number(e.target.value);
              onSelect(n === runs[total - 1].run ? null : n);
            }}
            className="h-8 max-w-[22rem] rounded-sm border border-input bg-card px-2 text-sm text-foreground tabular-nums"
          >
            {[...runs].reverse().map((r) => (
              <option key={r.run} value={r.run}>
                {runLabel(r, total)}
              </option>
            ))}
          </select>
        </>
      ) : (
        <span className="text-sm text-muted-foreground tabular-nums">{runLabel(selected, total)}</span>
      )}
      <InfoTip label="About traffic runs" align="end">
        {SHIFT_HELP.runs}
      </InfoTip>
    </div>
  );
}
