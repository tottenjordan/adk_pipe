"use client";

import { useEffect, useEffectEvent } from "react";
import { ShortcutKeys } from "@/components/shortcut-help";
import { SHORTCUTS } from "@/lib/shortcuts";

/* Shared bits of the interactive review panels. */

/**
 * Cmd/Ctrl+Enter (`SHORTCUTS.approve`) runs the panel's primary (approve) action from anywhere on
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

/** Inline hint for the approve shortcut (from the registry), shown next to the action buttons. */
export function ShortcutHint({ action = "approve" }: { action?: string }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
      <ShortcutKeys shortcut={SHORTCUTS.approve} /> to {action}
    </span>
  );
}

export const ACTIONS_ROW = "flex flex-wrap items-center gap-3";
