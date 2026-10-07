"use client";

import { useEffect, useState } from "react";
import { getCalibration } from "@/lib/api";
import { cn } from "@/lib/utils";
import { judgeAgreementText } from "@/lib/ratings";

/**
 * One muted line on the run history: how well the LLM judge agrees with your
 * ratings, or how many more creatives to rate first. Renders nothing while
 * loading or when the calibration endpoint is unavailable (fail soft).
 */
export function JudgeAgreement({ className }: { className?: string }) {
  const [text, setText] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getCalibration()
      .then((c) => {
        if (!cancelled) setText(judgeAgreementText(c));
      })
      .catch(() => {
        /* fail soft: no line */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!text) return null;
  return <p className={cn("text-sm text-muted-foreground", className)}>{text}</p>;
}
