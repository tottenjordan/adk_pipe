"use client";

import { useState } from "react";
import { captionPreview } from "@/lib/share-views";
import { ShareImage, type ShareCreativeProps } from "./share-image";

/** Generic, unbranded social feed post mock: header, image, headline + CTA bar, caption. */
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
      <ShareImage token={token} creative={creative} className="w-full" />
      <div className="flex items-center justify-between gap-3 border-y border-border bg-muted px-3 py-2.5">
        <div className="min-w-0">
          <h2 id={headingId} className="text-sm font-semibold leading-snug text-foreground">
            {creative.headline}
          </h2>
          <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{creative.body}</p>
        </div>
        {creative.cta && (
          <span className="shrink-0 rounded-sm border border-border bg-card px-3 py-1.5 text-sm font-semibold text-foreground">
            {creative.cta}
          </span>
        )}
      </div>
      {creative.caption && (
        <p className="px-3 py-2.5 text-sm leading-relaxed text-foreground">
          <span className="font-semibold">{brand}</span>{" "}
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
    </div>
  );
}
