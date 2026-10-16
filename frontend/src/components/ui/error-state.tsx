import * as React from "react";
import Link from "next/link";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

type ErrorStateProps = {
  /** Short headline, e.g. "Couldn't load runs". */
  title: string;
  /** What happened and what to do next. */
  message?: React.ReactNode;
  /** Shows a retry button when given. */
  onRetry?: () => void;
  retryLabel?: string;
  /** Optional way out, e.g. { href: "/", label: "Start a new run" }. */
  secondary?: { href: string; label: string };
  className?: string;
};

/** Load-failure panel: announced (role="alert"), with an optional retry and link. */
function ErrorState({
  title,
  message,
  onRetry,
  retryLabel = "Try again",
  secondary,
  className,
}: ErrorStateProps) {
  return (
    <div
      role="alert"
      data-slot="error-state"
      className={cn("flex flex-col items-start gap-2 rounded-lg border border-border bg-card p-4 text-sm", className)}
    >
      <p className="font-medium text-foreground">{title}</p>
      {message && <div className="text-muted-foreground">{message}</div>}
      {(onRetry || secondary) && (
        <div className="mt-1 flex items-center gap-3">
          {onRetry && (
            <Button variant="outline" size="sm" onClick={onRetry}>
              {retryLabel}
            </Button>
          )}
          {secondary && (
            <Link href={secondary.href} className="text-sm text-primary underline-offset-4 hover:underline">
              {secondary.label}
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

export { ErrorState };
export type { ErrorStateProps };
