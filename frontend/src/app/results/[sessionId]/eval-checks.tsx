import { FieldLabel } from "@/components/field-label";
import { cn } from "@/lib/utils";
import { gateLabel } from "@/lib/eval-dimensions";
import { scoreGates, type CreativeScore } from "@/lib/eval-matching";

/**
 * The judge's binary compliance checks for one eval: a row per gate with its
 * label, a pass/fail mark spelled out (never colour alone), an "advisory" tag
 * for checks that never fail a creative, and the judge's note. Reports
 * written before gates existed have none → renders nothing.
 */
export function ChecksList({
  score,
  note,
}: {
  score: CreativeScore;
  /** Optional context line under the checks (e.g. "judged from the prompt"). */
  note?: string;
}) {
  const gates = scoreGates(score);
  if (gates.length === 0) return null;
  return (
    <div className="mb-4">
      <FieldLabel as="h4">Checks</FieldLabel>
      <ul className="mt-1.5 space-y-1.5">
        {gates.map((g) => (
          <li key={g.gate} className="text-xs leading-snug">
            <div className="flex items-baseline gap-2">
              <span
                className={cn(
                  "w-8 shrink-0 font-semibold",
                  g.passed ? "text-mark-pass" : "text-mark-fail"
                )}
              >
                {g.passed ? "pass" : "fail"}
              </span>
              <span className="text-foreground">{gateLabel(g.gate)}</span>
              {g.advisory && <span className="text-muted-foreground">advisory</span>}
            </div>
            {g.note && <p className="mt-0.5 pl-10 text-muted-foreground">{g.note}</p>}
          </li>
        ))}
      </ul>
      {note && <p className="mt-1.5 text-xs text-muted-foreground">{note}</p>}
    </div>
  );
}

/** Small contact-sheet chip for a creative with a failed (blocking) check. */
export function CheckFailedChip({ count, className }: { count: number; className?: string }) {
  if (count <= 0) return null;
  return (
    <div className={className}>
      <span
        className="inline-flex items-center rounded-sm border border-mark-fail px-1.5 py-0.5 text-xs font-medium leading-none text-mark-fail"
        title={count === 1 ? "1 check failed" : `${count} checks failed`}
      >
        Check failed
      </span>
    </div>
  );
}
