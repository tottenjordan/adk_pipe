"use client";

import { useEffect, useEffectEvent } from "react";

/* Shared bits of the interactive review panels. */

/**
 * Cmd/Ctrl+Enter runs the panel's primary (approve) action from anywhere on
 * the page while the panel is shown. Skipped when `enabled` is false (e.g. no
 * trends selected yet), on key repeat, and when another handler already took it.
 */
export function useApproveShortcut(onApprove: () => void, enabled = true) {
  // Always the latest onApprove, without re-subscribing on every render.
  const approve = useEffectEvent(onApprove);
  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Enter" || !(e.metaKey || e.ctrlKey)) return;
      if (e.repeat || e.defaultPrevented) return;
      e.preventDefault();
      approve();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [enabled]);
}

/** Inline hint for the approve shortcut, shown next to the action buttons. */
export function ShortcutHint({ action = "approve" }: { action?: string }) {
  const key = "rounded-sm border border-border bg-muted px-1 font-sans text-xs text-foreground";
  return (
    <span className="text-xs text-muted-foreground">
      <kbd className={key}>Ctrl</kbd> <kbd className={key}>Enter</kbd> or{" "}
      <kbd className={key}>⌘</kbd> <kbd className={key}>Enter</kbd> to {action}
    </span>
  );
}

export const ACTIONS_ROW = "flex flex-wrap items-center gap-3";
