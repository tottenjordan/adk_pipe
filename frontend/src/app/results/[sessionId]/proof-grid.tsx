"use client";

import { useState, type Ref } from "react";
import { cn } from "@/lib/utils";
import { proofFailedChecks, type Proof, type ProofSort } from "@/lib/eval-matching";
import { SegmentedControl } from "@/components/segmented-control";
import { CheckFailedChip } from "./eval-checks";
import { CONDENSED, ScoreMark } from "./score-mark";

const SORT_OPTIONS: { value: ProofSort; label: string }[] = [
  { value: "pipeline", label: "Pipeline order" },
  { value: "highest", label: "Highest score" },
  { value: "lowest", label: "Lowest score" },
];

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

/**
 * Contact sheet: every creative as a dense proof (square image, headline, two
 * score marks). Each proof is a button that opens ProofDetail.
 */
export function ProofGrid({
  proofs,
  sort,
  onSortChange,
  imageUrlFor,
  onOpen,
  itemRef,
  isRated,
}: {
  /** Already sorted. */
  proofs: Proof[];
  sort: ProofSort;
  onSortChange: (sort: ProofSort) => void;
  imageUrlFor: (conceptName: string) => string | null;
  onOpen: (proofIndex: number) => void;
  itemRef: (proofIndex: number) => Ref<HTMLButtonElement>;
  /** Whether the user has rated this proof (shows a muted "Rated" mark). */
  isRated?: (proof: Proof) => boolean;
}) {
  return (
    <section aria-labelledby="proofs-heading" className="mb-6">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 id="proofs-heading" className="text-sm font-semibold text-foreground">
          Creatives{" "}
          <span className="font-normal text-muted-foreground tabular-nums">
            {proofs.length}
          </span>
        </h2>
        <SegmentedControl
          label="Sort creatives"
          options={SORT_OPTIONS}
          value={sort}
          onChange={onSortChange}
        />
      </div>

      <ul className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        {proofs.map((p) => {
          const rated = isRated?.(p) ?? false;
          return (
            <li key={p.index}>
              <button
                type="button"
                ref={itemRef(p.index)}
                onClick={() => onOpen(p.index)}
                aria-label={`Open creative ${p.index + 1}: ${p.concept.headline}${rated ? " (rated)" : ""}`}
                className="group flex h-full w-full flex-col rounded-lg border border-border bg-card p-2 text-left transition-colors hover:border-foreground/40"
              >
                <ProofImage
                  src={imageUrlFor(p.concept.concept_name)}
                  alt={p.concept.concept_summary}
                  cast={p.casting?.cast === true}
                  className="aspect-square w-full"
                />
                <div className="flex flex-1 flex-col px-1 pt-3 pb-1">
                  <h3
                    className={cn(
                      CONDENSED,
                      "line-clamp-3 text-xl leading-[1.1] text-foreground group-hover:underline group-hover:decoration-2 group-hover:underline-offset-2"
                    )}
                  >
                    {p.concept.headline}
                  </h3>
                  <p className="mt-1 flex gap-2 text-xs text-muted-foreground">
                    <span className="truncate">{p.concept.concept_name}</span>
                    {rated && <span className="ml-auto shrink-0">Rated</span>}
                  </p>
                  <div className="mt-auto pt-3">
                    <div className="grid grid-cols-2 gap-3 border-t border-border pt-3">
                      <ScoreMark label="Ad copy" score={p.adCopyEval?.score} />
                      <ScoreMark label="Visual" score={p.visualEval?.score} />
                    </div>
                  </div>
                  <CheckFailedChip count={proofFailedChecks(p).length} className="mt-2" />
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
