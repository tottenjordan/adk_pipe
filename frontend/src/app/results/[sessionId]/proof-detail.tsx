"use client";

import { type KeyboardEvent, type ReactNode } from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import type { Proof } from "@/lib/eval-matching";
import { ProofCopyPane } from "./proof-copy-pane";
import { ProofImagePane } from "./proof-image-pane";

/**
 * Full view of one proof in a dialog: large image left, copy and scores right.
 * Left/Right arrows and the prev/next buttons step through the sheet in its
 * current sort order; Escape closes and focus returns to the grid item.
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

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.target instanceof HTMLElement && e.target.closest("input, textarea, select")) return;
    if (e.key === "ArrowLeft" && hasPrev) {
      e.preventDefault();
      go(-1);
    } else if (e.key === "ArrowRight" && hasNext) {
      e.preventDefault();
      go(1);
    }
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
          className="flex max-h-[calc(100dvh-2rem)] w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-[min(72rem,calc(100%-2rem))] md:grid md:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] data-open:animate-none data-closed:animate-none"
        >
          <ProofImagePane proof={proof} imageUrlFor={imageUrlFor} />

          <div className="flex min-h-0 flex-1 flex-col md:h-[min(calc(100dvh-2rem),52rem)]">
            <div className="flex items-center gap-1 border-b border-border px-4 py-2 pr-12">
              <Button
                variant="outline"
                size="icon-sm"
                onClick={() => go(-1)}
                disabled={!hasPrev}
                aria-label="Previous creative"
              >
                <ChevronLeftIcon />
              </Button>
              <Button
                variant="outline"
                size="icon-sm"
                onClick={() => go(1)}
                disabled={!hasNext}
                aria-label="Next creative"
              >
                <ChevronRightIcon />
              </Button>
              <span className="ml-2 text-xs text-muted-foreground tabular-nums">
                {pos + 1} of {proofs.length}
              </span>
            </div>

            <ProofCopyPane
              proof={proof}
              ratingSlot={ratingSlot}
              shareAction={shareAction}
              personaliseSlot={personaliseSlot}
            />
          </div>
        </DialogContent>
      )}
    </Dialog>
  );
}
