"use client";

import { useEffect, useState } from "react";
import { getCalibration } from "@/lib/api";
import { cn } from "@/lib/utils";
import { judgeAgreementText, learningSplitText } from "@/lib/ratings";

/**
 * One muted line on the run history: how well the current LLM judge agrees with
 * your ratings, or how many more creatives to rate first (ratings judged by an
 * earlier judge version are noted, not counted). A second muted line compares
 * runs steered by rating learning with the rest once both have enough ratings.
 * Renders nothing while loading or when the calibration endpoint is unavailable
 * (fail soft).
 */
export function JudgeAgreement({ className }: { className?: string }) {
  const [text, setText] = useState<string | null>(null);
  const [split, setSplit] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getCalibration()
      .then((c) => {
        if (cancelled) return;
        setText(judgeAgreementText(c));
        setSplit(learningSplitText(c));
      })
      .catch(() => {
        /* fail soft: no line */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!text) return null;
  if (!split) return <p className={cn("text-sm text-muted-foreground", className)}>{text}</p>;
  return (
    <div className={cn("space-y-0.5", className)}>
      <p className="text-sm text-muted-foreground">{text}</p>
      <p className="text-sm text-muted-foreground">{split}</p>
    </div>
  );
}
