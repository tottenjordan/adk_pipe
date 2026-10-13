"use client";

import { useState } from "react";
import { cn } from "@/lib/utils";

/**
 * The proof image, or a quiet "Image not rendered" placeholder when there's no
 * URL or the image fails to load (e.g. a run that stopped before rendering, so
 * the file was never written and the GCS proxy 404s). A creative that cast a
 * consented person (`generated_images[c].cast`) whose image no longer loads was
 * deleted by revoking that consent, so it says so instead.
 */
export function ProofImage({
  src,
  alt,
  className,
  fit = "cover",
  cast = false,
}: {
  src: string | null;
  alt: string;
  className?: string;
  fit?: "cover" | "contain";
  /** The render cast a consented person (`generated_images[c].cast`). */
  cast?: boolean;
}) {
  // Keyed on the src that failed, so a new src (detail navigation) retries.
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  if (!src || failedSrc === src) {
    const removed = Boolean(src) && cast;
    return (
      <div
        className={cn(
          "flex items-center justify-center bg-muted text-sm text-muted-foreground",
          className
        )}
      >
        {removed ? "Image removed (consent revoked)" : "Image not rendered"}
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={alt}
      onError={() => setFailedSrc(src)}
      className={cn(
        "rounded-none bg-muted",
        fit === "cover" ? "object-cover" : "object-contain",
        className
      )}
    />
  );
}
