"use client";

import type { Ref } from "react";
import { cn } from "@/lib/utils";
import type { Proof, ProofSort } from "@/lib/eval-matching";
import { CONDENSED, ScoreMark } from "./score-mark";

const SORT_OPTIONS: { value: ProofSort; label: string }[] = [
  { value: "pipeline", label: "Pipeline order" },
  { value: "highest", label: "Highest score" },
  { value: "lowest", label: "Lowest score" },
];

/** The proof image, or the "No image available" fallback when there's no URL. */
export function ProofImage({
  src,
  alt,
  className,
  fit = "cover",
}: {
  src: string | null;
  alt: string;
  className?: string;
  fit?: "cover" | "contain";
}) {
  if (!src) {
    return (
      <div
        className={cn(
          "flex items-center justify-center bg-muted text-sm text-muted-foreground",
          className
        )}
      >
        No image available
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={alt}
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
}: {
  /** Already sorted. */
  proofs: Proof[];
  sort: ProofSort;
  onSortChange: (sort: ProofSort) => void;
  imageUrlFor: (conceptName: string) => string | null;
  onOpen: (proofIndex: number) => void;
  itemRef: (proofIndex: number) => Ref<HTMLButtonElement>;
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
        <div
          role="group"
          aria-label="Sort creatives"
          className="inline-flex rounded-sm border border-border bg-card p-0.5"
        >
          {SORT_OPTIONS.map((o) => {
            const active = o.value === sort;
            return (
              <button
                key={o.value}
                type="button"
                aria-pressed={active}
                onClick={() => onSortChange(o.value)}
                className={cn(
                  "h-7 rounded-sm px-2.5 text-xs font-medium transition-colors",
                  active
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground"
                )}
              >
                {o.label}
              </button>
            );
          })}
        </div>
      </div>

      <ul className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        {proofs.map((p) => (
          <li key={p.index}>
            <button
              type="button"
              ref={itemRef(p.index)}
              onClick={() => onOpen(p.index)}
              aria-label={`Open creative ${p.index + 1}: ${p.concept.headline}`}
              className="group flex h-full w-full flex-col rounded-lg border border-border bg-card p-2 text-left transition-colors hover:border-foreground/40"
            >
              <ProofImage
                src={imageUrlFor(p.concept.concept_name)}
                alt={p.concept.concept_summary}
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
                <p className="mt-1 truncate text-xs text-muted-foreground">
                  {p.concept.concept_name}
                </p>
                <div className="mt-auto pt-3">
                  <div className="grid grid-cols-2 gap-3 border-t border-border pt-3">
                    <ScoreMark label="Ad copy" score={p.adCopyEval?.score} />
                    <ScoreMark label="Visual" score={p.visualEval?.score} />
                  </div>
                </div>
              </div>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
