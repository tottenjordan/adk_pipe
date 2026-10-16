"use client";

import Link from "next/link";
import { RunList, useRunHistory } from "@/components/run-list";
import type { RunRow } from "@/lib/run-history";

const RECENT_RUNS = 5;

/** The New-run page's "Recent runs" sidebar: the newest runs with Duplicate brief. */
export function RecentRunsAside({ onDuplicate }: { onDuplicate: (row: RunRow) => void }) {
  const history = useRunHistory();
  const recentRows = history.rows.slice(0, RECENT_RUNS);

  return (
    <aside
      aria-labelledby="recent-runs-heading"
      className="rounded-lg border border-border bg-card"
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h2 id="recent-runs-heading" className="text-sm font-semibold text-foreground">
          Recent runs
        </h2>
        <Link href="/runs" className="text-xs text-primary underline-offset-4 hover:underline">
          See all runs
        </Link>
      </div>
      {history.loading ? (
        <p className="px-4 py-4 text-sm text-muted-foreground">Loading runs…</p>
      ) : history.error ? (
        <p className="px-4 py-4 text-sm text-muted-foreground">
          Couldn&apos;t load runs. Check your connection and reload.
        </p>
      ) : recentRows.length === 0 ? (
        <p className="px-4 py-4 text-sm text-muted-foreground">
          No runs yet. Fill in the brief to start your first one.
        </p>
      ) : (
        <RunList rows={recentRows} now={history.now} onDuplicate={onDuplicate} compact />
      )}
    </aside>
  );
}
