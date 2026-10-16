"use client";

import { useState, type KeyboardEvent, type ReactNode } from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import type { Proof } from "@/lib/eval-matching";
import { SHORTCUTS, hasCommandModifier, isTypingTarget } from "@/lib/shortcuts";
import { ProofCopyPane } from "./proof-copy-pane";
import { ProofImagePane } from "./proof-image-pane";
import { ProofKeysContext, dispatchProofKey, type ProofKeyRegistry } from "./proof-keys";

/**
 * Full view of one proof in a dialog: large image left, copy and scores right.
 * Left/Right arrows and the prev/next buttons step through the sheet in its
 * current sort order; Escape closes and focus returns to the grid item. Rating
 * keys (`lib/shortcuts.ts`, proof scope) go to the rating controls via
 * `proof-keys.ts`. Every key action is instant (nothing animates on a key), and
 * none fires while typing, with Ctrl/⌘/Alt/Shift held, or under another dialog.
 */
export function ProofDetail({
  proofs,
  open,
  index,
  onIndexChange,
  onClose,
  imageUrlFor,
  returnFocusTo,
  ratingSlot,
  shareSlot,
  personaliseSlot,
}: {
  /** The grid's current (sorted) order. */
  proofs: Proof[];
  open: boolean;
  /** Pipeline index of the shown proof (kept after close so focus can return). */
  index: number | null;
  onIndexChange: (index: number) => void;
  onClose: () => void;
  imageUrlFor: (conceptName: string) => string | null;
  returnFocusTo: (index: number) => HTMLElement | null;
  /** Extra section rendered under the scores (the human rating control). */
  ratingSlot?: (proof: Proof) => ReactNode;
  /** Share action rendered next to the rating slot ("Share this creative"). */
  shareSlot?: (proof: Proof) => ReactNode;
  /** Personalised preview panel, under the rating slot (rendered creatives; "Swap person" for cast ones). */
  personaliseSlot?: (proof: Proof) => ReactNode;
}) {
  const pos = index === null ? -1 : proofs.findIndex((p) => p.index === index);
  const proof = pos >= 0 ? proofs[pos] : undefined;
  const hasPrev = pos > 0;
  const hasNext = pos >= 0 && pos < proofs.length - 1;

  const go = (delta: number) => {
    const next = proofs[pos + delta];
    if (next) onIndexChange(next.index);
  };

  const [registry] = useState<ProofKeyRegistry>(() => new Set());

  // A React handler on the popup, not a document listener: Base UI's popup stops
  // arrow keys from bubbling past it. A dialog opened from the viewer (Share) is
  // portalled outside the popup's DOM, so its keys (which still bubble here
  // through the React tree) fail the contains() check and never page or rate.
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!(e.target instanceof Node) || !e.currentTarget.contains(e.target)) return;
    if (e.defaultPrevented || hasCommandModifier(e) || e.shiftKey || isTypingTarget(e.target)) return;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      const delta = e.key === "ArrowLeft" ? -1 : 1;
      if (delta < 0 ? !hasPrev : !hasNext) return;
      e.preventDefault();
      go(delta);
      return;
    }
    if (e.repeat) return;
    if (dispatchProofKey(registry, e.key.toLowerCase())) e.preventDefault();
  };

  const shareAction = proof ? shareSlot?.(proof) : null;

  return (
    <Dialog
      open={open && proof !== undefined}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      {proof && proof.concept && (
        <DialogContent
          onKeyDown={onKeyDown}
          finalFocus={() => returnFocusTo(proof.index)}
          className="flex max-h-[calc(100dvh-2rem)] w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-[min(72rem,calc(100%-2rem))] md:grid md:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]"
        >
          <ProofImagePane proof={proof} imageUrlFor={imageUrlFor} />

          <div className="flex min-h-0 flex-1 flex-col md:h-[min(calc(100dvh-2rem),52rem)]">
            <div className="flex items-center gap-1 border-b border-border px-4 py-2 pr-12">
              <Button
                variant="outline"
                size="icon"
                onClick={() => go(-1)}
                disabled={!hasPrev}
                aria-label={SHORTCUTS.prev.label}
                aria-keyshortcuts={SHORTCUTS.prev.aria}
              >
                <ChevronLeftIcon />
              </Button>
              <Button
                variant="outline"
                size="icon"
                onClick={() => go(1)}
                disabled={!hasNext}
                aria-label={SHORTCUTS.next.label}
                aria-keyshortcuts={SHORTCUTS.next.aria}
              >
                <ChevronRightIcon />
              </Button>
              <span className="ml-2 text-xs text-muted-foreground tabular-nums">
                {pos + 1} of {proofs.length}
              </span>
              {/* Visual hint only: the buttons carry aria-keyshortcuts. */}
              <span className="ml-3 flex items-center gap-1" aria-hidden="true">
                <Kbd>{SHORTCUTS.prev.combos[0][0]}</Kbd>
                <Kbd>{SHORTCUTS.next.combos[0][0]}</Kbd>
              </span>
            </div>

            <ProofKeysContext value={registry}>
              <ProofCopyPane
                proof={proof}
                ratingSlot={ratingSlot}
                shareAction={shareAction}
                personaliseSlot={personaliseSlot}
              />
            </ProofKeysContext>
          </div>
        </DialogContent>
      )}
    </Dialog>
  );
}
