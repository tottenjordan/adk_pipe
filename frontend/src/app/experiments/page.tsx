"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";
import { ExperimentStatusLabel, StatusHelp } from "@/components/experiment-status";
import { InfoTip } from "@/components/ui/info-tip";
import { formatRunTime } from "@/lib/run-history";
import {
  armName,
  canStop,
  listExperiments,
  scenarioLabel,
  ttlText,
  type ExperimentSummary,
} from "@/lib/experiments";
import { cn } from "@/lib/utils";

const COLUMNS =
  "sm:grid sm:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)_8.5rem_6.5rem_9rem] sm:items-center sm:gap-4";

export default function ExperimentsPage() {
  const [rows, setRows] = useState<ExperimentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [now, setNow] = useState(0);

  useEffect(() => {
    let cancelled = false;
    listExperiments()
      .then((list) => {
        if (cancelled) return;
        setRows(list);
        setNow(Date.now());
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setError(true);
        setLoading(false);
      });
    // Keep TTL countdowns current without refetching.
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  return (
    <div className="mx-auto w-full max-w-6xl px-6 py-8">
      <div className="mb-4">
        <h1 className="text-2xl font-semibold text-foreground">Experiments</h1>
        <p className="text-sm text-muted-foreground">
          {!loading && !error && rows.length > 0
            ? `${rows.length} ${rows.length === 1 ? "experiment" : "experiments"}, newest first. One can be live at a time.`
            : "Creatives deployed as live bandit experiments."}
        </p>
      </div>

      <section aria-label="Experiments" className="rounded-lg border border-border bg-card">
        {loading ? (
          <p className="px-4 py-6 text-sm text-muted-foreground" role="status">
            Loading experiments…
          </p>
        ) : error ? (
          <p role="alert" className="px-4 py-6 text-sm text-mark-fail">
            Couldn&apos;t load experiments. Check your connection and reload.
          </p>
        ) : rows.length === 0 ? (
          <div className="px-4 py-6 text-sm text-muted-foreground">
            <p>No experiments yet.</p>
            <p className="mt-1">
              Open a finished creative run from{" "}
              <Link href="/runs" className="text-primary underline-offset-4 hover:underline">
                Runs
              </Link>{" "}
              and use <span className="text-foreground">Deploy creatives as a live experiment</span>{" "}
              below the creatives.
            </p>
          </div>
        ) : (
          <>
            {/* Visual column headers; only the status help button is exposed to assistive tech. */}
            <div
              className={cn(
                "hidden border-b border-border px-4 py-2 text-xs font-medium text-muted-foreground",
                COLUMNS
              )}
            >
              <span aria-hidden>Scenario</span>
              <span aria-hidden>Creatives</span>
              <span className="inline-flex items-center gap-1">
                <span aria-hidden>Status</span>
                <InfoTip label="About experiment status">
                  <StatusHelp />
                </InfoTip>
              </span>
              <span aria-hidden>Created</span>
              <span aria-hidden>Lifetime</span>
            </div>
            <ul className="divide-y divide-border">
              {rows.map((row) => {
                const names = [...row.arms].sort((a, b) => a.index - b.index).map(armName);
                const live = canStop(row.status);
                return (
                  <li
                    key={row.experimentId}
                    className={cn(
                      "relative flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5 text-sm hover:bg-muted/60 has-[a:focus-visible]:ring-3 has-[a:focus-visible]:ring-ring/50 has-[a:focus-visible]:ring-inset",
                      COLUMNS
                    )}
                  >
                    <Link
                      href={`/experiments/${encodeURIComponent(row.experimentId)}`}
                      prefetch={false}
                      className="truncate font-medium text-foreground after:absolute after:inset-0 after:content-[''] focus-visible:outline-none"
                    >
                      {scenarioLabel(row.scenario)}
                    </Link>
                    <span className="truncate text-foreground/80" title={names.join(", ")}>
                      <span className="tabular-nums">
                        {row.arms.length} {row.arms.length === 1 ? "creative" : "creatives"}:
                      </span>{" "}
                      <span className="text-muted-foreground">{names.join(", ")}</span>
                    </span>
                    <ExperimentStatusLabel status={row.status} />
                    <span className="text-xs whitespace-nowrap text-muted-foreground tabular-nums">
                      {formatRunTime(Date.parse(row.createdAt) || null, now)}
                    </span>
                    <span className="text-xs whitespace-nowrap text-muted-foreground tabular-nums">
                      {live ? ttlText(row.ttlExpiresAt, now) || "No limit" : "Endpoint removed"}
                    </span>
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </section>
      {!loading && !error && rows.length > 0 && (
        <p className="mt-3 text-xs text-muted-foreground">
          Deploy another from a creative run&apos;s results page.{" "}
          <Link href="/runs" className={cn(buttonVariants({ variant: "link", size: "xs" }), "px-0")}>
            Open runs
          </Link>
        </p>
      )}
    </div>
  );
}
