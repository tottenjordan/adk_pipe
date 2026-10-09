import { cn } from "@/lib/utils";
import { SHARE_CONDENSED, ShareImage, type ShareCreativeProps } from "./share-image";

/** Proof card: the image beside the copy, read in order headline → body → caption → CTA. */
export function ShareCard({ token, creative, headingId }: ShareCreativeProps) {
  return (
    <div className="grid gap-6 border border-border bg-card p-4 md:grid-cols-2 md:p-6">
      <ShareImage token={token} creative={creative} className="w-full" />
      <div className="flex min-w-0 flex-col">
        <h2 id={headingId} className={cn(SHARE_CONDENSED, "text-3xl leading-[1.05] text-foreground")}>
          {creative.headline}
        </h2>
        <p className="mt-3 text-base leading-relaxed text-foreground">{creative.body}</p>
        {creative.caption && (
          <p className="mt-3 text-sm leading-relaxed text-muted-foreground">{creative.caption}</p>
        )}
        {creative.cta && (
          // A mock call to action: styled like a button, deliberately not one.
          <span className="mt-5 inline-flex w-fit items-center rounded-sm border-2 border-foreground px-3 py-1.5 text-sm font-semibold text-foreground">
            {creative.cta}
          </span>
        )}
        {(creative.tone || creative.visual_style) && (
          <p className="mt-auto pt-5 text-xs text-muted-foreground">
            {[creative.tone && `Tone: ${creative.tone}`, creative.visual_style && `Style: ${creative.visual_style}`]
              .filter(Boolean)
              .join(" · ")}
          </p>
        )}
      </div>
    </div>
  );
}
