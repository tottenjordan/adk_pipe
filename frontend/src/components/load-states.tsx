"use client";

import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { isSessionExpired, SESSION_EXPIRED_MESSAGE } from "@/lib/session-expired";
import { cn } from "@/lib/utils";

/**
 * A list's load failure: "Couldn't load <what>" with a retry, or, when the IAP
 * sign-in expired (lib/session-expired), the one session-expired message with a
 * reload (retrying the fetch can't sign anyone back in).
 */
export function LoadErrorState({
  what,
  error,
  onRetry,
  detail,
  className,
}: {
  /** "runs", "experiments", … */
  what: string;
  error: unknown;
  onRetry: () => void;
  /** Overrides the default "check your connection" line. */
  detail?: string;
  className?: string;
}) {
  if (isSessionExpired(error)) {
    return (
      <ErrorState
        className={className}
        title={SESSION_EXPIRED_MESSAGE}
        onRetry={() => window.location.reload()}
        retryLabel="Reload"
      />
    );
  }
  return (
    <ErrorState
      className={className}
      title={`Couldn't load ${what}.`}
      message={detail ?? "Check your connection and try again."}
      onRetry={onRetry}
    />
  );
}

/**
 * Loading rows shaped like a list: one bar per column (widths as Tailwind
 * classes), an optional square thumbnail, inside a polite status with an
 * sr-only label.
 */
export function ListSkeleton({
  label,
  rows = 5,
  columns,
  thumb = false,
  className,
}: {
  label: string;
  rows?: number;
  columns: string[];
  thumb?: boolean;
  className?: string;
}) {
  return (
    <div role="status" className={cn("divide-y divide-border", className)}>
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-4 px-4 py-3">
          {thumb && <Skeleton className="size-14 shrink-0" />}
          {columns.map((w, j) => (
            <Skeleton key={j} className={cn("h-3.5", w)} />
          ))}
        </div>
      ))}
    </div>
  );
}
