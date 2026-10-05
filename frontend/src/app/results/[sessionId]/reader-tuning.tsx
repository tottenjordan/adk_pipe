"use client";

import { useMemo, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { FieldLabel } from "@/components/field-label";
import { InfoTip } from "@/components/ui/info-tip";
import { Slider } from "@/components/ui/slider";
import { formatPercent } from "@/lib/chart";
import { cellShade } from "@/lib/creative-detail";
import { TUNE_HELP } from "@/lib/experiment-help";
import type { CtrMode, Scenario } from "@/lib/experiments";
import {
  displayPercents,
  formatScale,
  judgeLabel,
  MIX_FLOOR,
  OVERRIDE_BOUNDS,
  overridesFromValues,
  presetValues,
  previewMatrix,
  previewReading,
  rebalanceMix,
  SCENARIO_PRESETS,
  segmentWords,
  type ArmScores,
  type TuneValues,
} from "@/lib/scenario-preview";
import { cn } from "@/lib/utils";
import { SHADE_MIN, SHADE_SPAN } from "@/app/experiments/[experimentId]/segment-grid";
import { CONDENSED } from "./score-mark";

export interface TuningCreative {
  /** Pipeline index (the arm order the experiment page colours by). */
  index: number;
  name: string;
  /** selectionColors(): the colour this creative keeps on the experiment page. */
  color: string;
  scores: ArmScores;
}

/**
 * Deploy panel "Advanced: tune the simulated readers" (contracts §9): sliders for
 * the scenario's reader knobs, filled from the preset, plus a live preview of the
 * expected click rate per creative × segment (lib/scenario-preview.ts).
 */
export function ReaderTuning({
  scenario,
  ctrMode,
  values,
  onChange,
  creatives,
}: {
  scenario: Scenario;
  ctrMode: CtrMode;
  values: TuneValues;
  onChange: (v: TuneValues) => void;
  /** The selected creatives, in pipeline order. */
  creatives: TuningCreative[];
}) {
  const preset = SCENARIO_PRESETS[scenario];
  const base = presetValues(scenario);
  const changed = Object.keys(overridesFromValues(scenario, values) ?? {}).length;
  const set = (patch: Partial<TuneValues>) => onChange({ ...values, ...patch });
  const percents = displayPercents(values.segmentMix);
  const mixMax = 1 - MIX_FLOOR * (values.segmentMix.length - 1);

  return (
    <Collapsible className="mt-5 border-t border-border pt-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <CollapsibleTrigger className="group inline-flex items-center gap-1.5 rounded-sm text-sm font-semibold text-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
          <ChevronRight
            aria-hidden
            className="size-4 text-muted-foreground transition-transform group-data-[panel-open]:rotate-90 motion-reduce:transition-none"
          />
          Advanced: tune the simulated readers
        </CollapsibleTrigger>
        <span className="text-xs text-muted-foreground">
          {changed === 0
            ? "Using the scenario's defaults"
            : `${changed} ${changed === 1 ? "setting" : "settings"} changed from the scenario's defaults`}
        </span>
      </div>

      <CollapsibleContent>
        <div className="mt-2 flex flex-wrap items-start justify-between gap-3">
          <p className="max-w-[68ch] text-sm text-muted-foreground">{TUNE_HELP.section}</p>
          <Button
            variant="outline"
            size="xs"
            onClick={() => onChange(base)}
            disabled={changed === 0}
            title={TUNE_HELP.reset}
          >
            Reset to preset
          </Button>
        </div>

        <div className="mt-5 grid gap-x-10 gap-y-8 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
          {/* Knobs */}
          <div className="space-y-7">
            <fieldset>
              <div className="mb-2 flex items-center gap-1">
                <FieldLabel as="legend" className="float-left">
                  Audience mix
                </FieldLabel>
                <InfoTip label="About the audience mix" align="start">
                  {TUNE_HELP.mix}
                </InfoTip>
              </div>
              <ul className="clear-left space-y-1">
                {preset.segments.map((seg, i) => {
                  const label = segmentWords(seg.name);
                  return (
                    <li key={seg.name} className="grid grid-cols-[minmax(7rem,9rem)_1fr_2.75rem] items-center gap-3">
                      <span className="truncate text-sm text-foreground">{label}</span>
                      <Slider
                        aria-label={`${label} share of readers`}
                        value={values.segmentMix[i]}
                        min={MIX_FLOOR}
                        max={mixMax}
                        step={0.01}
                        largeStep={0.1}
                        marks={[base.segmentMix[i]]}
                        valueText={() => `${percents[i]}% of readers`}
                        onValueChange={(v) => set({ segmentMix: rebalanceMix(values.segmentMix, i, v) })}
                      />
                      <span className="text-right text-sm font-medium text-foreground tabular-nums">{percents[i]}%</span>
                    </li>
                  );
                })}
              </ul>
            </fieldset>

            <Knob
              id="tune-gap"
              label="Gap between creatives"
              help={TUNE_HELP.gap}
              display={formatScale(values.gapScale)}
              ends={["Subtle", "Obvious"]}
            >
              <Slider
                aria-labelledby="tune-gap"
                value={values.gapScale}
                min={OVERRIDE_BOUNDS.gapScale[0]}
                max={OVERRIDE_BOUNDS.gapScale[1]}
                step={0.05}
                largeStep={0.25}
                marks={[base.gapScale]}
                valueText={(v) => `${formatScale(v)} the preset gap`}
                onValueChange={(v) => set({ gapScale: v })}
              />
            </Knob>

            <Knob
              id="tune-judge"
              label="Judge reliability"
              help={TUNE_HELP.judge}
              display={judgeLabel(values.judgeWrong)}
              ends={["Judge is right", "No information", "Judge is backwards"]}
              note={judgeNote(values.judgeWrong)}
            >
              <Slider
                aria-labelledby="tune-judge"
                value={values.judgeWrong}
                min={OVERRIDE_BOUNDS.judgeWrong[0]}
                max={OVERRIDE_BOUNDS.judgeWrong[1]}
                step={0.05}
                largeStep={0.25}
                fill={false}
                marks={[0.5]}
                valueText={judgeLabel}
                onValueChange={(v) => set({ judgeWrong: v })}
              />
            </Knob>

            <Knob
              id="tune-noise"
              label="Random variation"
              help={TUNE_HELP.noise}
              display={values.noiseScale === 0 ? "None" : formatScale(values.noiseScale)}
              ends={["None", "Double"]}
            >
              <Slider
                aria-labelledby="tune-noise"
                value={values.noiseScale}
                min={OVERRIDE_BOUNDS.noiseScale[0]}
                max={OVERRIDE_BOUNDS.noiseScale[1]}
                step={0.1}
                largeStep={0.5}
                marks={[base.noiseScale]}
                valueText={(v) => (v === 0 ? "No random variation" : `${formatScale(v)} the preset variation`)}
                onValueChange={(v) => set({ noiseScale: v })}
              />
            </Knob>

            {scenario === "drift" && (
              <Knob
                id="tune-drift"
                label="Drift point"
                help={TUNE_HELP.drift}
                display={`${Math.round(values.driftAtFrac * 100)}% of the run`}
                ends={["20%", "80%"]}
              >
                <Slider
                  aria-labelledby="tune-drift"
                  value={values.driftAtFrac}
                  min={OVERRIDE_BOUNDS.driftAtFrac[0]}
                  max={OVERRIDE_BOUNDS.driftAtFrac[1]}
                  step={0.05}
                  largeStep={0.1}
                  marks={[base.driftAtFrac]}
                  valueText={(v) => `${Math.round(v * 100)}% of the run`}
                  onValueChange={(v) => set({ driftAtFrac: v })}
                />
              </Knob>
            )}
          </div>

          {/* Live preview */}
          <PreviewGrid scenario={scenario} ctrMode={ctrMode} values={values} creatives={creatives} />
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function judgeNote(jw: number): string | null {
  if (jw <= 0.001) return TUNE_HELP.judgeRight;
  if (Math.abs(jw - 0.5) <= 0.001) return TUNE_HELP.judgeNone;
  if (jw >= 0.999) return TUNE_HELP.judgeBackwards;
  return null;
}

function Knob({
  id,
  label,
  help,
  display,
  ends,
  note,
  children,
}: {
  id: string;
  label: string;
  help: string;
  display: string;
  ends: string[];
  note?: string | null;
  children: ReactNode;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <span className="flex items-center gap-1">
          <FieldLabel id={id}>{label}</FieldLabel>
          <InfoTip label={`About ${label.toLowerCase()}`} align="start">
            {help}
          </InfoTip>
        </span>
        <span className="text-sm font-medium text-foreground tabular-nums">{display}</span>
      </div>
      {children}
      <div aria-hidden className="flex justify-between gap-2 text-xs text-muted-foreground">
        {ends.map((e) => (
          <span key={e}>{e}</span>
        ))}
      </div>
      {note && <p className="mt-1.5 text-xs leading-snug text-foreground">{note}</p>}
    </div>
  );
}

function PreviewGrid({
  scenario,
  ctrMode,
  values,
  creatives,
}: {
  scenario: Scenario;
  ctrMode: CtrMode;
  values: TuneValues;
  creatives: TuningCreative[];
}) {
  const arms = useMemo(() => creatives.map((c) => c.scores), [creatives]);
  const pv = useMemo(
    () => (arms.length >= 2 ? previewMatrix({ scenario, ctrMode, arms, values }) : null),
    [scenario, ctrMode, arms, values]
  );
  const names = useMemo(() => creatives.map((c) => c.name), [creatives]);
  const reading = useMemo(() => (pv ? previewReading(pv, names, segmentWords) : ""), [pv, names]);

  const header = (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <span className="flex items-center gap-1">
        <h3 className="text-sm font-semibold text-foreground">Expected click rate by segment</h3>
        <InfoTip label="About the preview" align="start">
          {TUNE_HELP.preview}
        </InfoTip>
      </span>
      <span className="text-xs text-muted-foreground">Expected rates before random variation</span>
    </div>
  );

  if (!pv) {
    return (
      <div>
        {header}
        <p className="mt-3 rounded-md border border-dashed border-border px-4 py-8 text-center text-sm text-muted-foreground">
          Pick at least two creatives above to preview how these readers respond to them.
        </p>
      </div>
    );
  }

  const all = pv.ctr.flat();
  const range: [number, number] = [Math.min(...all), Math.max(...all)];
  const percents = displayPercents(pv.weights);
  const cols = `minmax(8.5rem,1.35fr) repeat(${pv.segments.length}, minmax(4.25rem,1fr))`;

  return (
    <div>
      {header}
      <div className="mt-3 overflow-x-auto rounded-md border border-border">
        <table className="w-full min-w-[26rem] border-collapse text-sm">
          <caption className="sr-only">
            Expected click rate of each selected creative with each audience segment under these settings, before
            random variation; the best creative per segment is marked.
          </caption>
          <thead>
            <tr className="grid border-b border-border" style={{ gridTemplateColumns: cols }}>
              <th scope="col" className="px-3 py-2 text-left text-xs font-normal text-muted-foreground">
                Creative
              </th>
              {pv.segments.map((s, i) => (
                <th key={s} scope="col" className="px-1.5 py-2 text-center text-xs font-normal leading-tight text-muted-foreground">
                  <span className="block text-foreground/80">{segmentWords(s)}</span>
                  <span className="tabular-nums">{percents[i]}% of readers</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {creatives.map((c, a) => {
              const color = c.color;
              return (
                <tr key={c.index} className="grid border-b border-border last:border-b-0" style={{ gridTemplateColumns: cols }}>
                  <th scope="row" className="flex min-w-0 items-center gap-2 px-3 py-1 text-left font-semibold text-foreground">
                    <span aria-hidden className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: color }} />
                    <span className="line-clamp-2 leading-tight" title={c.name}>
                      {c.name}
                    </span>
                  </th>
                  {pv.segments.map((s, si) => {
                    const v = pv.ctr[a][si];
                    const best = pv.oracle[si] === a;
                    return (
                      <td key={s} className="p-1">
                        <div
                          className="relative flex h-12 items-center justify-center rounded-[3px] text-foreground tabular-nums"
                          style={{
                            backgroundColor: `rgb(26 29 33 / ${(SHADE_MIN + cellShade(v, range) * SHADE_SPAN).toFixed(3)})`,
                            boxShadow: best ? `inset 0 0 0 2px ${color}` : undefined,
                          }}
                        >
                          <span className={cn(CONDENSED, "text-[1.25rem] leading-none")}>
                            {formatPercent(v, ctrMode === "realistic" ? 2 : 1)}
                          </span>
                          {best && (
                            <span className="absolute top-0.5 right-1 text-[10px] leading-none font-semibold" style={{ color }}>
                              Best
                              <span className="sr-only"> for this segment</span>
                            </span>
                          )}
                        </div>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {reading && (
        <p className="mt-3 max-w-[68ch] text-base leading-snug text-foreground">
          {reading}
        </p>
      )}
    </div>
  );
}
