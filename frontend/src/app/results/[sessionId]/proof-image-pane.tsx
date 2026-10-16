"use client";

import type { Proof } from "@/lib/eval-matching";
import { ProofImage } from "./proof-grid";

/** The proof viewer's left-hand pane: the rendered image, contained on the light table. */
export function ProofImagePane({
  proof,
  imageUrlFor,
}: {
  proof: Proof;
  imageUrlFor: (conceptName: string) => string | null;
}) {
  const vc = proof.concept;
  return (
    <div className="flex h-[38dvh] shrink-0 items-center justify-center bg-foreground/[0.04] md:h-[min(calc(100dvh-2rem),52rem)]">
      <ProofImage
        src={imageUrlFor(vc.concept_name)}
        alt={vc.concept_summary}
        cast={proof.casting?.cast === true}
        fit="contain"
        className="h-full w-full"
      />
    </div>
  );
}
