"use client";

import { useState } from "react";
import { captionPreview } from "@/lib/share-views";
import { ShareImage, type ShareCreativeProps } from "./share-image";

/**
 * Generic, unbranded social feed ad mock, laid out like a feed link ad: header, primary
 * text (the caption), the image, then a full-width link bar with the brand, headline,
 * body and CTA. The line clamps are visual only; the full text stays in the DOM.
 */
export function FeedPost({ token, brand, creative, headingId }: ShareCreativeProps) {
  const [expanded, setExpanded] = useState(false);
  const preview = captionPreview(creative.caption);
  const initial = brand.trim().charAt(0).toUpperCase() || "·";
  return (
    <div className="mx-auto w-full max-w-[470px] border border-border bg-card">
      <div className="flex items-center gap-3 px-3 py-2.5">
        <span
          data-slot="feed-avatar"
          aria-hidden="true"
          className="flex size-9 shrink-0 items-center justify-center rounded-full bg-foreground text-sm font-semibold text-card"
        >
          {initial}
        </span>
        <div className="min-w-0 leading-tight">
          <p className="truncate text-sm font-semibold text-foreground">{brand}</p>
          <p className="text-xs text-muted-foreground">Sponsored</p>
        </div>
      </div>
      {creative.caption && (
        <p className="px-3 pb-2.5 text-sm leading-relaxed text-foreground">
          <span>{expanded || !preview.truncated ? creative.caption : `${preview.text}…`}</span>
          {preview.truncated && !expanded && (
            <>
              {" "}
              <button
                type="button"
                aria-expanded={false}
                onClick={() => setExpanded(true)}
                className="rounded-sm font-medium text-muted-foreground underline-offset-2 hover:underline"
              >
                more
              </button>
            </>
          )}
        </p>
      )}
      <ShareImage token={token} creative={creative} className="w-full" />
      <div
        data-slot="feed-link-bar"
        className="grid grid-cols-[minmax(0,1fr)_fit-content(calc(40%_-_0.75rem))] items-center gap-3 border-t border-border bg-muted px-3 py-3 max-[360px]:grid-cols-1"
      >
        <div className="min-w-0">
          <p className="truncate text-xs text-muted-foreground">{brand}</p>
          <h2
            id={headingId}
            className="mt-0.5 line-clamp-2 text-[15px] font-semibold leading-snug text-foreground"
          >
            {creative.headline}
          </h2>
          {creative.body && (
            <p className="mt-0.5 line-clamp-2 text-sm leading-snug text-muted-foreground">
              {creative.body}
            </p>
          )}
        </div>
        {creative.cta && (
          <div data-slot="feed-cta" className="max-[360px]:w-full">
            <span className="block whitespace-normal break-words rounded-sm border border-border bg-card px-4 py-2 text-center text-sm font-semibold leading-snug text-foreground">
              {creative.cta}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
