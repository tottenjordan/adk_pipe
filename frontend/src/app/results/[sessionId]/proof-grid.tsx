"use client";

import type { Ref } from "react";
import { cn } from "@/lib/utils";
import { proofFailedChecks, type Proof, type ProofSort, type Timely } from "@/lib/eval-matching";
import { SegmentedControl } from "@/components/segmented-control";
import { ProofImage } from "@/components/proof-image";
import { CheckFailedChip } from "./eval-checks";
import { CONDENSED, ScoreMark } from "./score-mark";

// Re-exported for existing importers; the component lives in components/proof-image.
export { ProofImage };

const SORT_OPTIONS: { value: ProofSort; label: string }[] = [
  { value: "pipeline", label: "Pipeline order" },
  { value: "highest", label: "Highest score" },
  { value: "lowest", label: "Lowest score" },
];

/** The proof button's aria-label suffix for the timely creative ("" otherwise). */
export function timelyAria(timely: Timely | undefined): string {
  if (!timely) return "";
  return timely.expired ? " · timely, expired" : " · timely";
}

/**
 * Small contact-sheet chip for the timely creative: "Timely" (valid until … in
 * its title and for screen readers), or "Expired" once its moment has passed.
 */
export function TimelyChip({ timely, className }: { timely?: Timely; className?: string }) {
  if (!timely) return null;
  const detail = timely.validUntil
    ? `${timely.expired ? "expired on" : "valid until"} ${timely.validUntil}`
    : "";
  return (
    <div className={className}>
      <span
        className={cn(
          "inline-flex items-center rounded-sm border px-1.5 py-0.5 text-xs font-medium leading-none",
          timely.expired ? "border-mark-pending text-mark-pending" : "border-mark-pass text-mark-pass"
        )}
        title={detail || undefined}
      >
        {timely.expired ? "Expired" : "Timely"}
        {detail && <span className="sr-only">{`, ${detail}`}</span>}
      </span>
    </div>
  );
}

/**
 * Small mark-pending chip for a creative whose ad copy names the real person
 * of a sensitive trend (a respectful tribute; check rights before live use).
 */
export function NamesRealPersonChip({ show, className }: { show?: boolean; className?: string }) {
  if (!show) return null;
  return (
    <div className={className}>
      <span className="inline-flex items-center rounded-sm border border-mark-pending px-1.5 py-0.5 text-xs font-medium leading-none text-mark-pending">
        Names a real person
      </span>
    </div>
  );
}

/** Whether a proof shows any flag chip under its scores. */
function hasFlags(p: Proof): boolean {
  return proofFailedChecks(p).length > 0 || Boolean(p.timely) || Boolean(p.namesRealPerson);
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
  // One card with a chip reserves the flag row on every card, so scores line up.
  const reserveFlags = proofs.some(hasFlags);
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
                aria-label={`Open creative ${p.index + 1}: ${p.concept.headline}${timelyAria(p.timely)}${p.namesRealPerson ? " · names a real person" : ""}${rated ? " (rated)" : ""}`}
                className="group flex h-full w-full flex-col rounded-lg border border-border bg-card p-2 text-left transition-[border-color,scale] duration-(--dur-press) ease-(--ease-out) hover:border-foreground/40 active:scale-[0.99] motion-reduce:transition-colors motion-reduce:active:scale-100"
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
                  {reserveFlags && (
                    <div data-slot="proof-flags" className="mt-2 flex min-h-4.5 flex-wrap gap-1.5">
                      <CheckFailedChip count={proofFailedChecks(p).length} />
                      <TimelyChip timely={p.timely} />
                      <NamesRealPersonChip show={p.namesRealPerson} />
                    </div>
                  )}
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
