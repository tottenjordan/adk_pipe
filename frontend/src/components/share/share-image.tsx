import { cn } from "@/lib/utils";
import type { ShareCreative } from "@/lib/share-snapshot";
import { cssAspectRatio, imageAlt, shareImageSrc } from "@/lib/share-views";

/** Condensed heavy display face for headlines (Archivo width axis). */
export const SHARE_CONDENSED = "[font-stretch:75%] font-extrabold tracking-tight";

/** A share creative's frozen image (same-origin `/s/<token>/img/<i>`). */
export function ShareImage({
  token,
  creative,
  className,
  fill = false,
}: {
  token: string;
  creative: ShareCreative;
  className?: string;
  /** Fill the parent (story frame) instead of keeping the creative's aspect ratio. */
  fill?: boolean;
}) {
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={shareImageSrc(token, creative.index)}
      alt={imageAlt(creative.alt, creative.headline)}
      className={cn("block bg-muted object-cover", className)}
      style={fill ? undefined : { aspectRatio: cssAspectRatio(creative.aspect_ratio) }}
      decoding="async"
    />
  );
}

export interface ShareCreativeProps {
  token: string;
  brand: string;
  creative: ShareCreative;
  /** id of this creative's h2 (the article's accessible name). */
  headingId: string;
}
