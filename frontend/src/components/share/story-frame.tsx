import { cn } from "@/lib/utils";
import { SHARE_CONDENSED, ShareImage, type ShareCreativeProps } from "./share-image";

/**
 * 9:16 story mock. The overlay (brand, headline, CTA) sits on a black scrim that is at
 * least 70% opaque behind the text, so white text meets WCAG AA on any image. The overlay
 * is decorative for assistive tech; the full copy follows the frame as real text.
 */
export function StoryFrame({ token, brand, creative, headingId }: ShareCreativeProps) {
  return (
    <div className="mx-auto flex w-full max-w-[300px] flex-col gap-4">
      <div
        data-slot="story-frame"
        className="relative aspect-[9/16] w-full overflow-hidden rounded-[24px] border-[6px] border-foreground bg-foreground"
      >
        <ShareImage token={token} creative={creative} fill className="absolute inset-0 h-full w-full" />
        <div data-slot="story-overlay" aria-hidden="true">
          <div className="absolute inset-x-0 top-0 flex items-center gap-2 bg-linear-to-b from-black/75 to-transparent px-3 pt-3 pb-8 text-white">
            <span className="flex size-7 items-center justify-center rounded-full bg-white text-xs font-semibold text-black">
              {brand.trim().charAt(0).toUpperCase() || "·"}
            </span>
            <span className="truncate text-xs font-semibold">{brand}</span>
            <span className="text-xs">Sponsored</span>
          </div>
          <div className="absolute inset-x-0 bottom-0 bg-linear-to-t from-black/90 via-black/75 via-70% to-transparent px-4 pt-16 pb-6 text-white">
            <p className={cn(SHARE_CONDENSED, "text-2xl leading-[1.05]")}>{creative.headline}</p>
            {creative.cta && (
              <span className="mt-3 inline-flex rounded-full bg-white px-4 py-1.5 text-sm font-semibold text-black">
                {creative.cta}
              </span>
            )}
          </div>
        </div>
      </div>
      <div className="min-w-0">
        <h2 id={headingId} className={cn(SHARE_CONDENSED, "text-xl leading-[1.1] text-foreground")}>
          {creative.headline}
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-foreground">{creative.body}</p>
        {creative.caption && (
          <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{creative.caption}</p>
        )}
        {creative.cta && (
          <span className="mt-3 inline-flex w-fit rounded-sm border-2 border-foreground px-3 py-1 text-sm font-semibold text-foreground">
            {creative.cta}
          </span>
        )}
      </div>
    </div>
  );
}
