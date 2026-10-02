import { cn } from "@/lib/utils";
import type { CreativeScore } from "@/lib/eval-matching";

/** Condensed heavy type for creative headlines and score marks (Archivo wdth axis). */
export const CONDENSED = "[font-stretch:75%] font-extrabold tracking-tight";

/** 0–1 score → whole percent, rounded the same way the old page did (toFixed(0)). */
export function pct(score: number): string {
  return (score * 100).toFixed(0);
}

/**
 * A creative's overall score as a grease-pencil mark: big condensed number in
 * pass green / fail red, with the label and the pass/fail word spelled out so
 * the result never depends on colour alone.
 */
export function ScoreMark({
  label,
  score,
  size = "md",
  className,
}: {
  label: string;
  score?: CreativeScore;
  size?: "md" | "lg";
  className?: string;
}) {
  const tone = !score
    ? "text-muted-foreground"
    : score.passed
      ? "text-mark-pass"
      : "text-mark-fail";
  return (
    <div className={cn("min-w-0", className)}>
      <div
        className={cn(
          CONDENSED,
          "leading-none tabular-nums",
          size === "lg" ? "text-4xl" : "text-3xl",
          tone
        )}
      >
        {score ? (
          <>
            {pct(score.overall_score)}
            <span className="text-[0.55em]">%</span>
          </>
        ) : (
          "–"
        )}
      </div>
      <p className="mt-1 text-xs leading-none text-muted-foreground">
        {label}{" "}
        <span className={cn("font-medium", tone)}>
          {!score ? "not scored" : score.passed ? "pass" : "fail"}
        </span>
      </p>
    </div>
  );
}
