import { FieldLabel } from "@/components/field-label";
import { cn } from "@/lib/utils";
import type { ShareCheck, ShareEval as ShareEvalData } from "@/lib/share-snapshot";

const KIND_PREFIX = { copy: "Copy", visual: "Visual" } as const;

/** Copy and visual share gate names (e.g. avoid list), so name the half. */
function checkLabel(c: ShareCheck): string {
  return c.kind ? `${KIND_PREFIX[c.kind]}: ${c.label}` : c.label;
}

/**
 * The judge's checks for one shared creative (only when the owner included them):
 * a pass/fail mark spelled out per check (never colour alone), advisory checks
 * labelled, plus the overall verdict and score.
 */
export function ShareEval({ data }: { data: ShareEvalData }) {
  return (
    <section aria-label="Judge checks" className="mt-4 border-t border-border pt-3">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <FieldLabel as="h3">Checks</FieldLabel>
        <span
          className={cn(
            "text-sm font-semibold",
            data.passed === null ? "text-muted-foreground" : data.passed ? "text-mark-pass" : "text-mark-fail",
          )}
        >
          {data.passed === null ? "Not judged" : data.passed ? "Passed" : "Did not pass"}
        </span>
        {data.score !== null && (
          <span className="text-sm text-muted-foreground">
            Score <span className="font-semibold text-foreground">{Math.round(data.score * 100)}%</span>
          </span>
        )}
      </div>
      {data.checks.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {data.checks.map((c) => (
            <li key={`${c.kind ?? ""}:${c.gate}`} className="flex items-baseline gap-2 text-xs leading-snug">
              <span className={cn("w-8 shrink-0 font-semibold", c.passed ? "text-mark-pass" : "text-mark-fail")}>
                {c.passed ? "pass" : "fail"}
              </span>
              <span className="text-foreground">{checkLabel(c)}</span>
              {c.advisory && <span className="text-muted-foreground">advisory</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
