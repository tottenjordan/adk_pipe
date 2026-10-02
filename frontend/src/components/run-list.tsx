"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { listSessions, SELF_USER_ID } from "@/lib/api";
import {
  buildRunRows,
  formatRunTime,
  HISTORY_LIST_APP,
  prepareRunView,
  type RunRow,
  type RunStatus,
} from "@/lib/run-history";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface RunHistory {
  rows: RunRow[];
  loading: boolean;
  error: boolean;
  /** Reference time the rows' statuses and relative dates were computed at. */
  now: number;
}

/**
 * Load the caller's runs once. All apps share one session store, so a single
 * `listSessions` call (under any app name) returns every run.
 */
export function useRunHistory(): RunHistory {
  const [history, setHistory] = useState<RunHistory>({
    rows: [],
    loading: true,
    error: false,
    now: 0,
  });

  useEffect(() => {
    let cancelled = false;
    listSessions(HISTORY_LIST_APP, SELF_USER_ID)
      .then((sessions) => {
        if (cancelled) return;
        const now = Date.now();
        setHistory({ rows: buildRunRows(sessions, now), loading: false, error: false, now });
      })
      .catch(() => {
        if (!cancelled) setHistory((h) => ({ ...h, loading: false, error: true }));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return history;
}

const DOT: Record<RunStatus, string> = {
  Completed: "bg-mark-pass",
  Failed: "bg-mark-fail",
  "Needs review": "bg-mark-pending",
  Running: "bg-muted-foreground",
  Incomplete: "bg-muted-foreground/50",
  "Not started": "bg-muted-foreground/30",
};

const STATUS_TEXT: Partial<Record<RunStatus, string>> = {
  Completed: "text-mark-pass",
  Failed: "text-mark-fail",
  "Needs review": "text-mark-pending",
};

export function RunStatusLabel({ status }: { status: RunStatus }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-xs font-medium whitespace-nowrap",
        STATUS_TEXT[status] ?? "text-muted-foreground",
      )}
    >
      <span aria-hidden className={cn("size-2 shrink-0 rounded-full", DOT[status])} />
      {status}
    </span>
  );
}

interface RunListProps {
  rows: RunRow[];
  now: number;
  onDuplicate: (row: RunRow) => void;
  /** Compact: two-line rows for a narrow sidebar; otherwise table-like columns. */
  compact?: boolean;
}

const COLUMNS =
  "sm:grid sm:grid-cols-[minmax(0,1.2fr)_minmax(0,1.4fr)_minmax(0,1fr)_7.5rem_6.5rem_7.5rem] sm:items-center sm:gap-4";

/** Column headings for the full (non-compact) list. */
export function RunListHeader() {
  return (
    <div
      aria-hidden
      className={cn(
        "hidden px-4 py-2 text-xs font-medium text-muted-foreground border-b border-border",
        COLUMNS,
      )}
    >
      <span>Brand</span>
      <span>Trend</span>
      <span>Agent</span>
      <span>Status</span>
      <span>Updated</span>
      <span />
    </div>
  );
}

export function RunList({ rows, now, onDuplicate, compact = false }: RunListProps) {
  return (
    <ul className="divide-y divide-border">
      {rows.map((row) => {
        const title = row.brand || "Untitled run";
        const time = formatRunTime(row.updatedAt, now);
        const link = (
          <Link
            href={row.href}
            prefetch={false}
            onClick={() => prepareRunView(row)}
            className="truncate font-medium text-foreground after:absolute after:inset-0 after:content-[''] focus-visible:outline-none"
          >
            {title}
          </Link>
        );
        const duplicate = (
          <Button
            variant="ghost"
            size="xs"
            className="relative z-10 text-muted-foreground"
            onClick={() => onDuplicate(row)}
            aria-label={`Duplicate brief from ${title}`}
          >
            Duplicate brief
          </Button>
        );

        if (compact) {
          return (
            <li
              key={row.id}
              className="group relative px-4 py-2 text-sm hover:bg-muted has-[a:focus-visible]:ring-3 has-[a:focus-visible]:ring-ring/50 has-[a:focus-visible]:ring-inset"
            >
              <div className="flex items-center gap-3">
                {link}
                <span className="ml-auto shrink-0">
                  <RunStatusLabel status={row.status} />
                </span>
              </div>
              <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                <span className="truncate">
                  {[row.agentLabel, row.trend].filter(Boolean).join(" · ")}
                </span>
                <span className="ml-auto shrink-0 tabular-nums">{time}</span>
              </div>
              {/* Revealed on hover/focus so the sidebar stays two lines a row;
                  still a real tab stop for keyboard users. */}
              <span className="absolute right-2 bottom-1.5 bg-card group-hover:bg-muted opacity-0 group-hover:opacity-100 has-[:focus-visible]:opacity-100">
                {duplicate}
              </span>
            </li>
          );
        }

        return (
          <li
            key={row.id}
            className={cn(
              "relative flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5 text-sm hover:bg-muted/60 has-[a:focus-visible]:ring-3 has-[a:focus-visible]:ring-ring/50 has-[a:focus-visible]:ring-inset",
              COLUMNS,
            )}
          >
            {link}
            <span className="truncate text-foreground/80" title={row.trend || undefined}>
              {row.trend || <span className="text-muted-foreground">No trend</span>}
            </span>
            <span className="truncate text-muted-foreground">{row.agentLabel}</span>
            <RunStatusLabel status={row.status} />
            <span className="text-xs text-muted-foreground tabular-nums whitespace-nowrap">
              {time}
            </span>
            <span className="sm:justify-self-end">{duplicate}</span>
          </li>
        );
      })}
    </ul>
  );
}
