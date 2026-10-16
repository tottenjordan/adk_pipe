"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

/** How long a "Link copied" confirmation stays visible. */
export const COPY_FEEDBACK_MS = 2000;

/**
 * A short-lived confirmation ("Link copied" / "Couldn't copy"): `show(text)`
 * sets it and clears it after COPY_FEEDBACK_MS (a newer one restarts the clock).
 * `key` says which control it belongs to, for lists with one Copy per row.
 */
export function useCopyFeedback() {
  const [feedback, setFeedback] = useState<{ text: string; key: string }>({ text: "", key: "" });
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    []
  );

  const show = useCallback((text: string, key = "") => {
    if (timer.current) clearTimeout(timer.current);
    setFeedback({ text, key });
    timer.current = setTimeout(() => setFeedback({ text: "", key: "" }), COPY_FEEDBACK_MS);
  }, []);

  return { ...feedback, show };
}

/**
 * The visible confirmation next to a Copy button. Always mounted (empty when
 * idle) so the polite live region exists before its text arrives.
 */
export function CopyFeedback({
  text,
  className,
  ...props
}: { text: string; className?: string } & React.ComponentProps<"span">) {
  return (
    <span aria-live="polite" className={cn("text-xs text-muted-foreground", className)} {...props}>
      {text}
    </span>
  );
}
