"use client";

import { Fragment, useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { isShareChromeless } from "@/components/site-header";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { SHORTCUTS, hasCommandModifier, isTypingTarget, shortcutsByScope, type Shortcut } from "@/lib/shortcuts";

/** A shortcut's key combos as caps: "Ctrl Enter or ⌘ Enter", ranges like 1–5 collapsed. */
export function ShortcutKeys({ shortcut }: { shortcut: Shortcut }) {
  const combos = shortcut.combos;
  const singleKeys = combos.every((c) => c.length === 1);
  if (singleKeys && combos.length > 2) {
    return (
      <span className="inline-flex items-center gap-1">
        <Kbd>{combos[0][0]}</Kbd>–<Kbd>{combos[combos.length - 1][0]}</Kbd>
      </span>
    );
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {combos.map((combo, i) => (
        <Fragment key={combo.join("+")}>
          {i > 0 && <span className="text-muted-foreground">or</span>}
          {combo.map((cap) => (
            <Kbd key={cap}>{cap}</Kbd>
          ))}
        </Fragment>
      ))}
    </span>
  );
}

/**
 * The `?` keyboard-shortcut help: a dialog listing the registry
 * (`lib/shortcuts.ts`) by scope, opened by `?` on any page (not while typing,
 * not with Ctrl/⌘/Alt held) or by the footer's "Keyboard shortcuts" button.
 * Mounted once in the root layout; renders nothing (and listens for nothing) on
 * the public share pages, which keep their own minimal UI.
 */
export function ShortcutHelp() {
  const pathname = usePathname();
  const enabled = !isShareChromeless(pathname);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "?" || e.repeat || e.defaultPrevented || hasCommandModifier(e)) return;
      if (isTypingTarget(e.target)) return;
      e.preventDefault();
      setOpen(true);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [enabled]);

  if (!enabled) return null;
  return (
    <footer className="mx-auto flex w-full max-w-[1600px] justify-end px-6 py-3">
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-keyshortcuts={SHORTCUTS.help.aria}
        className="inline-flex items-center gap-1.5 rounded-sm text-xs text-muted-foreground hover:text-foreground"
      >
        Keyboard shortcuts <Kbd aria-hidden="true">?</Kbd>
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Keyboard shortcuts</DialogTitle>
            <DialogDescription>
              Single keys don&apos;t fire while you type in a field or hold Ctrl, ⌘ or Alt. In the
              proof viewer, rating keys rate the visual unless focus is in the ad copy rating.
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-[70dvh] space-y-4 overflow-y-auto">
            {shortcutsByScope().map((group) => (
              <section key={group.scope} aria-labelledby={`shortcuts-${group.scope}`}>
                <h3
                  id={`shortcuts-${group.scope}`}
                  className="mb-1.5 text-sm font-semibold text-foreground"
                >
                  {group.title}
                </h3>
                <dl className="divide-y divide-border">
                  {group.items.map((s) => (
                    <div key={s.label} className="flex items-center justify-between gap-4 py-1.5">
                      <dt className="text-sm text-foreground">{s.label}</dt>
                      <dd className="shrink-0">
                        <ShortcutKeys shortcut={s} />
                      </dd>
                    </div>
                  ))}
                </dl>
              </section>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </footer>
  );
}
