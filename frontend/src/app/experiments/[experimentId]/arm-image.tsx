"use client";

import { ProofImage } from "@/components/proof-image";
import { armImageUrl, type Arm } from "@/lib/experiments";

/**
 * An experiment arm's image. A cast arm (`consentId` set, contracts §5) whose
 * image no longer loads was deleted by revoking that consent, so it shows
 * ProofImage's "Image removed (consent revoked)" placeholder.
 */
export function ArmImage({
  arm,
  alt,
  className,
}: {
  arm: Pick<Arm, "imageUri" | "consentId"> | null | undefined;
  alt: string;
  className?: string;
}) {
  return (
    <ProofImage
      src={arm ? armImageUrl(arm) : null}
      alt={alt}
      cast={Boolean(arm?.consentId)}
      className={className}
    />
  );
}
