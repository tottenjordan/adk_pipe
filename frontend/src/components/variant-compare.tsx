"use client";

import { useState } from "react";
import { ExternalLinkIcon } from "lucide-react";
import { ProofImage } from "@/components/proof-image";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type Side = { src: string | null; alt: string; caption: string };

/**
 * The finished personalised preview next to its original. The two thumbnails open a
 * large side-by-side view (a dialog nested in the proof detail, so Escape closes only
 * the viewer), each image fitted to the screen with a link to the full-size render.
 */
export function VariantCompare({ original, preview }: { original: Side; preview: Side }) {
  const [open, setOpen] = useState(false);
  const sides = [original, preview];

  return (
    <>
      <div className="mt-3 grid grid-cols-2 gap-2">
        {sides.map((side) => (
          <figure key={side.caption} className="min-w-0">
            <button
              type="button"
              onClick={() => setOpen(true)}
              disabled={!side.src}
              aria-label={`View larger: ${side.caption}`}
              className="block w-full cursor-zoom-in rounded-sm outline-hidden focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default"
            >
              <ProofImage
                src={side.src}
                alt={side.alt}
                fit="contain"
                className="aspect-square w-full rounded-sm bg-foreground/[0.04]"
              />
            </button>
            <figcaption className="mt-1 text-xs text-muted-foreground">{side.caption}</figcaption>
          </figure>
        ))}
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-[min(96vw,100rem)]">
          <DialogHeader>
            <DialogTitle>Compare previews</DialogTitle>
            <DialogDescription>
              Original and personalised preview, fitted to the screen. Open either at full
              size for every pixel.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 md:grid-cols-2">
            {sides.map((side) => (
              <figure key={side.caption} className="min-w-0">
                <ProofImage
                  src={side.src}
                  alt={side.alt}
                  fit="contain"
                  className="max-h-[calc(100dvh-12rem)] w-full rounded-sm bg-foreground/[0.04] md:h-[calc(100dvh-12rem)]"
                />
                <figcaption className="mt-2 flex items-center justify-between gap-2 text-xs">
                  <span className="text-muted-foreground">{side.caption}</span>
                  {side.src && (
                    <a
                      href={side.src}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 font-medium text-primary underline-offset-2 hover:underline"
                    >
                      Open full size
                      <ExternalLinkIcon className="size-3" aria-hidden="true" />
                      <span className="sr-only">: {side.caption} (opens in a new tab)</span>
                    </a>
                  )}
                </figcaption>
              </figure>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
