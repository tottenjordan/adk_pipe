"use client";

import { useId, useState } from "react";
import { BanIcon, CheckIcon, ChevronDownIcon } from "lucide-react";
import { FieldLabel } from "@/components/field-label";
import { fitModeLabel, type CreativeBrief as Brief } from "@/lib/creative-brief";
import type { ReportSources } from "@/lib/research-report";
import { cn } from "@/lib/utils";

const BODY = "text-sm leading-relaxed text-foreground/85";

/** One labelled field; renders nothing when empty. */
function Field({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={className}>
      <FieldLabel as="dt">{label}</FieldLabel>
      <dd className={cn("mt-1", BODY)}>{children}</dd>
    </div>
  );
}

/** A plain list with a quiet leading glyph (include / exclude), or nothing. */
function MarkList({ items, kind }: { items: string[]; kind: "include" | "exclude" }) {
  const Icon = kind === "include" ? CheckIcon : BanIcon;
  return (
    <ul className="space-y-1">
      {items.map((item, i) => (
        <li key={`${i}-${item}`} className="flex gap-2">
          <Icon aria-hidden="true" className="mt-[0.3em] size-3.5 shrink-0 text-muted-foreground" />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}

/** Five ticks, `score` of them inked: a quiet fit gauge next to "n/5". */
function FitMeter({ score }: { score: number }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span aria-hidden="true" className="inline-flex gap-0.5">
        {[1, 2, 3, 4, 5].map((n) => (
          <span
            key={n}
            data-filled={n <= score}
            className={cn("h-3 w-1.5 rounded-[1px]", n <= score ? "bg-foreground/80" : "bg-rule")}
          />
        ))}
      </span>
      <span className="text-sm font-semibold tabular-nums text-foreground">
        <span className="sr-only">Fit </span>
        {score}/5
      </span>
    </span>
  );
}

function sourceLabel(id: string, sources: ReportSources | undefined): string {
  if (id === "brief") return "From the brief";
  const src = sources?.[id];
  return src?.domain || src?.title || "";
}

/**
 * The structured creative brief, read-only. The single-minded proposition
 * leads (it is the one idea every creative must carry), then the insight,
 * then the supporting fields in a compact grid and the creative angles.
 */
export function CreativeBrief({
  brief,
  sources,
  className,
}: {
  brief: Brief;
  /** Research sources (`state.sources`), to name the `src-N` ids. */
  sources?: ReportSources;
  className?: string;
}) {
  const { trendBridge: tb, brand } = brief;
  const hasTrend = tb.fitScore !== null || tb.fitMode || tb.bridge || tb.motifs.length > 0 || tb.risks.length > 0;
  const hasBrand = brand.toneOfVoice || brand.distinctiveAssets.length > 0 || brand.doNot.length > 0;
  const basics = [
    { label: "Objective", value: brief.objective },
    { label: "Audience", value: brief.audience },
    { label: "Desired response", value: brief.desiredResponse },
  ].filter((f) => f.value);

  return (
    <div className={cn("space-y-5", className)}>
      <div className="max-w-[68ch]">
        <FieldLabel as="p">Single-minded proposition</FieldLabel>
        <p className="mt-1 text-xl leading-snug font-bold text-balance text-foreground sm:text-2xl">
          {brief.singleMindedProposition}
        </p>
        {brief.insight && (
          <div className="mt-3">
            <FieldLabel as="p">Insight</FieldLabel>
            <p className="mt-1 text-base leading-relaxed text-foreground/85">{brief.insight}</p>
          </div>
        )}
      </div>

      {basics.length > 0 && (
        <dl className="grid gap-x-6 gap-y-3 border-t border-border pt-4 sm:grid-cols-3">
          {basics.map((f) => (
            <Field key={f.label} label={f.label}>
              {f.value}
            </Field>
          ))}
        </dl>
      )}

      {(hasTrend || brief.reasonsToBelieve.length > 0) && (
        <dl className="grid gap-x-6 gap-y-4 border-t border-border pt-4 md:grid-cols-2">
          {hasTrend && (
            <Field label="Trend fit">
              {(tb.fitScore !== null || tb.fitMode) && (
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  {tb.fitScore !== null && <FitMeter score={tb.fitScore} />}
                  {tb.fitMode && (
                    <span className="text-sm font-medium text-foreground">{fitModeLabel(tb.fitMode)}</span>
                  )}
                </div>
              )}
              {tb.bridge && <p className="mt-1.5">{tb.bridge}</p>}
              {tb.motifs.length > 0 && (
                <div className="mt-2">
                  <FieldLabel>Motifs</FieldLabel>
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    {tb.motifs.map((m, i) => (
                      <span
                        key={`${i}-${m}`}
                        className="rounded-sm border border-border bg-muted/60 px-1.5 py-0.5 text-xs text-foreground"
                      >
                        {m}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {tb.risks.length > 0 && (
                <div className="mt-2">
                  <FieldLabel>Risks</FieldLabel>
                  <p className="mt-1">{tb.risks.join("; ")}</p>
                </div>
              )}
            </Field>
          )}

          {brief.reasonsToBelieve.length > 0 && (
            <Field label="Reasons to believe">
              <ul className="space-y-2">
                {brief.reasonsToBelieve.map((r, i) => {
                  const named = r.sourceId ? sourceLabel(r.sourceId, sources) : "";
                  return (
                    <li key={`${i}-${r.claim}`}>
                      {r.claim}
                      {r.sourceId && (
                        <span className="mt-0.5 block text-xs text-muted-foreground">
                          {r.sourceId !== "brief" && <span className="font-mono">{r.sourceId}</span>}
                          {r.sourceId !== "brief" && named && " "}
                          {named}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </Field>
          )}
        </dl>
      )}

      {(hasBrand || brief.mandatories.length > 0 || brief.avoid.length > 0) && (
        <dl className="grid gap-x-6 gap-y-4 border-t border-border pt-4 sm:grid-cols-2 lg:grid-cols-3">
          {hasBrand && (
            <Field label="Brand">
              {brand.toneOfVoice && <p>Tone: {brand.toneOfVoice}</p>}
              {brand.distinctiveAssets.length > 0 && (
                <p className="mt-1">Show: {brand.distinctiveAssets.join(", ")}</p>
              )}
              {brand.doNot.length > 0 && (
                <div className="mt-2">
                  <FieldLabel>Never</FieldLabel>
                  <div className="mt-1">
                    <MarkList items={brand.doNot} kind="exclude" />
                  </div>
                </div>
              )}
            </Field>
          )}
          {brief.mandatories.length > 0 && (
            <Field label="Must include">
              <MarkList items={brief.mandatories} kind="include" />
            </Field>
          )}
          {brief.avoid.length > 0 && (
            <Field label="Keep out">
              <MarkList items={brief.avoid} kind="exclude" />
            </Field>
          )}
        </dl>
      )}

      {brief.angles.length > 0 && (
        <section className="border-t border-border pt-4">
          <FieldLabel as="p">Creative angles</FieldLabel>
          <ul className="mt-2 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {brief.angles.map((a, i) => (
              <li key={`${i}-${a.angleId}`} className="rounded-lg border border-border bg-card px-3.5 py-3">
                <p className="flex items-baseline gap-2">
                  {a.angleId && (
                    <span className="font-mono text-xs text-muted-foreground">{a.angleId}</span>
                  )}
                  <span className="text-sm font-bold text-foreground">{a.name || "Untitled angle"}</span>
                </p>
                {a.tension && <p className="mt-1 text-sm text-foreground/85 italic">{a.tension}</p>}
                {a.route && <p className="mt-1.5 text-sm leading-relaxed text-foreground/85">{a.route}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

/**
 * The brief as an "outputs so far" tile on the run page: the proposition
 * up front, the full brief one click away.
 */
export function CreativeBriefOutput({
  brief,
  sources,
  className,
}: {
  brief: Brief;
  sources?: ReportSources;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  return (
    <div className={cn("rounded-lg border border-border bg-card px-4 py-3", className)}>
      <FieldLabel as="p">Creative brief</FieldLabel>
      <p className="mt-0.5 text-sm font-semibold text-balance text-foreground">
        {brief.singleMindedProposition}
      </p>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((o) => !o)}
        className="mt-1.5 inline-flex items-center gap-1 rounded-sm text-sm font-medium text-primary hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        {open ? "Hide the full brief" : "Show the full brief"}
        <ChevronDownIcon aria-hidden="true" className={cn("size-4 transition-transform", open && "rotate-180")} />
      </button>
      <div id={panelId} hidden={!open} className="mt-3 border-t border-border pt-4">
        {open && <CreativeBrief brief={brief} sources={sources} />}
      </div>
    </div>
  );
}
