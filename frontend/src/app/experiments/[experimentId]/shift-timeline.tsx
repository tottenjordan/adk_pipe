"use client";

import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronDownIcon, ChevronUpIcon, PlusIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FieldLabel } from "@/components/field-label";
import { InfoTip } from "@/components/ui/info-tip";
import { Slider } from "@/components/ui/slider";
import { formatPercent } from "@/lib/chart";
import { cellShade } from "@/lib/creative-detail";
import { SHIFT_HELP } from "@/lib/experiment-help";
import type { ShiftKind } from "@/lib/experiments";
import { displayPercents, formatPts, rebalanceMix } from "@/lib/scenario-preview";
import {
  changeKind,
  describeShift,
  KIND_LABELS,
  MAX_SHIFTS,
  moveShift,
  newShift,
  presetShift,
  scenarioSegments,
  segmentPhrase,
  SHIFT_KINDS,
  SHIFT_LEADER,
  SHIFT_MIN_WINDOW,
  SHIFT_PRESETS,
  shiftBounds,
  shiftPreview,
  swapTimes,
  timeOrder,
  validateShifts,
  type EditorShift,
  type ShiftContext,
  type ShiftError,
  type ShiftPresetId,
} from "@/lib/shifts";
import { cn } from "@/lib/utils";
import { CONDENSED } from "@/app/results/[sessionId]/score-mark";
import { SHADE_MIN, SHADE_SPAN } from "./segment-grid";

const INK = "#1a1d21";
const pct = (v: number) => `${Math.round(v * 100)}%`;

/**
 * The shift editor (contracts §10), in the control panel above Start traffic:
 * a timeline of the run with one draggable pin per event (each pin is a native
 * range input), an "Add shift" menu (four kinds, three presets), one compact
 * form and plain-language sentence per event, the forgetting toggle, and a live
 * before/after preview of the expected click rates.
 */
export function ShiftTimeline({
  ctx,
  shifts,
  onChange,
  forget,
  onForgetChange,
  disabled = false,
  serverError,
}: {
  ctx: ShiftContext;
  shifts: EditorShift[];
  onChange: (next: EditorShift[]) => void;
  forget: boolean;
  onForgetChange: (on: boolean) => void;
  disabled?: boolean;
  /** The api's `invalid_shifts` field, if the last start was rejected. */
  serverError?: string | null;
}) {
  const ordered = useMemo(() => timeOrder(shifts), [shifts]);
  const errors = useMemo(() => validateShifts(shifts, ctx), [shifts, ctx]);
  const pv = useMemo(() => (shifts.length ? shiftPreview(ctx, shifts) : null), [ctx, shifts]);
  const color = (id: string | undefined, listIndex: number) => {
    const resolved = id === SHIFT_LEADER ? pv?.resolvedNames[listIndex] : undefined;
    const c = resolved
      ? ctx.creatives.find((x) => x.name === resolved)
      : ctx.creatives.find((x) => x.creativeId === id);
    return c?.color ?? INK;
  };

  const update = (key: string, fn: (s: EditorShift) => EditorShift) =>
    onChange(shifts.map((s) => (s.key === key ? fn(s) : s)));
  const remove = (key: string) => onChange(shifts.filter((s) => s.key !== key));
  const add = (kind: ShiftKind | null, preset: ShiftPresetId | null) => {
    if (shifts.length >= MAX_SHIFTS) return;
    const next = preset ? presetShift(preset, ctx, shifts) : newShift(kind as ShiftKind, ctx, shifts);
    onChange([...shifts, next]);
  };
  const swap = (a: EditorShift, b: EditorShift) =>
    onChange(swapTimes(shifts, shifts.indexOf(a), shifts.indexOf(b)));

  return (
    <section aria-labelledby="shifts-heading" className="border-b border-border pb-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1">
          <h3 id="shifts-heading" className="text-sm font-semibold text-foreground">
            Behaviour shifts
          </h3>
          <InfoTip label="About behaviour shifts">{SHIFT_HELP.section}</InfoTip>
          <span className="ml-2 text-xs text-muted-foreground tabular-nums">
            {shifts.length ? `${shifts.length} of ${MAX_SHIFTS}` : "Optional"}
          </span>
        </div>
        <AddShiftMenu onAdd={add} disabled={disabled || shifts.length >= MAX_SHIFTS} />
      </div>

      {shifts.length === 0 ? (
        <p className="mt-2 max-w-[68ch] text-sm text-muted-foreground">
          Script a change in what readers want partway through the run, then watch how the endpoint adapts. Every
          strategy sees the same change; a dashed line shows the endpoint without it.
        </p>
      ) : (
        <>
          <Timeline
            ordered={ordered}
            shifts={shifts}
            color={color}
            disabled={disabled}
            onMove={(key, field, frac) =>
              update(key, (s) =>
                field === "atFrac"
                  ? moveShift(s, frac)
                  : { ...s, untilFrac: Math.max(frac, s.atFrac + SHIFT_MIN_WINDOW) }
              )
            }
          />

          <ol className="mt-4 divide-y divide-border rounded-md border border-border">
            {ordered.map((s, i) => (
              <ShiftForm
                key={s.key}
                n={i + 1}
                shift={s}
                ctx={ctx}
                color={color(s.creativeId, shifts.indexOf(s))}
                sentence={describeShift(s, ctx, pv?.resolvedNames[shifts.indexOf(s)])}
                errors={errors.filter((e) => e.index === shifts.indexOf(s))}
                disabled={disabled}
                onChange={(next) => update(s.key, () => next)}
                onRemove={() => remove(s.key)}
                onEarlier={i > 0 ? () => swap(s, ordered[i - 1]) : undefined}
                onLater={i < ordered.length - 1 ? () => swap(s, ordered[i + 1]) : undefined}
              />
            ))}
          </ol>

          <label className="mt-4 flex max-w-[68ch] cursor-pointer items-start gap-2.5">
            <input
              type="checkbox"
              checked={forget}
              disabled={disabled}
              onChange={(e) => onForgetChange(e.target.checked)}
              className="mt-0.5 size-4 shrink-0 accent-[var(--primary)]"
            />
            <span>
              <span className="text-sm font-medium text-foreground">Let the endpoint forget old evidence</span>
              <span className="block text-xs leading-snug text-muted-foreground">{SHIFT_HELP.forget}</span>
            </span>
          </label>

          {pv && pv.regimes.length > 1 && <PreviewStrip pv={pv} ctx={ctx} />}
        </>
      )}
      {serverError && (
        <p role="alert" className="mt-3 text-sm text-mark-fail">
          {serverError}
        </p>
      )}
    </section>
  );
}

// ── Add menu ─────────────────────────────────────────────────────────────────

function AddShiftMenu({
  onAdd,
  disabled,
}: {
  onAdd: (kind: ShiftKind | null, preset: ShiftPresetId | null) => void;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const menuId = useId();
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const pick = (kind: ShiftKind | null, preset: ShiftPresetId | null) => {
    onAdd(kind, preset);
    setOpen(false);
  };
  return (
    <div ref={ref} className="relative">
      <Button
        variant="outline"
        size="sm"
        disabled={disabled}
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => setOpen((o) => !o)}
      >
        <PlusIcon /> Add shift
      </Button>
      {open && (
        <div
          id={menuId}
          role="menu"
          className="absolute right-0 z-20 mt-1 w-72 rounded-md border border-border bg-popover p-1 text-sm shadow-md"
        >
          <p className="px-2.5 pt-1.5 pb-1 text-xs text-muted-foreground">What changes</p>
          {SHIFT_KINDS.map((k) => (
            <button
              key={k}
              type="button"
              role="menuitem"
              onClick={() => pick(k, null)}
              className="block w-full rounded-sm px-2.5 py-1.5 text-left text-foreground outline-none hover:bg-muted focus-visible:bg-muted"
            >
              {KIND_LABELS[k]}
            </button>
          ))}
          <div className="my-1 border-t border-border" />
          <p className="px-2.5 pt-1 pb-1 text-xs text-muted-foreground">Ready-made</p>
          {SHIFT_PRESETS.map((p) => (
            <button
              key={p.id}
              type="button"
              role="menuitem"
              onClick={() => pick(null, p.id)}
              className="block w-full rounded-sm px-2.5 py-1.5 text-left outline-none hover:bg-muted focus-visible:bg-muted"
            >
              <span className="block text-foreground">{p.label}</span>
              <span className="block text-xs text-muted-foreground">{p.description}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Timeline ─────────────────────────────────────────────────────────────────

/** The native range thumb is invisible but sized to the pin, so dragging the pin drags the input. */
const THUMB =
  "[&::-webkit-slider-thumb]:pointer-events-auto [&::-webkit-slider-thumb]:h-16 [&::-webkit-slider-thumb]:w-7 [&::-webkit-slider-thumb]:cursor-grab [&::-webkit-slider-thumb]:appearance-none [&::-moz-range-thumb]:pointer-events-auto [&::-moz-range-thumb]:h-16 [&::-moz-range-thumb]:w-7 [&::-moz-range-thumb]:cursor-grab [&::-moz-range-thumb]:border-0";

function Timeline({
  ordered,
  shifts,
  color,
  disabled,
  onMove,
}: {
  ordered: EditorShift[];
  shifts: EditorShift[];
  color: (id: string | undefined, listIndex: number) => string;
  disabled: boolean;
  onMove: (key: string, field: "atFrac" | "untilFrac", frac: number) => void;
}) {
  const [focus, setFocus] = useState<string | null>(null);
  return (
    <div className="mt-4 select-none">
      <div className="relative mx-3.5 h-[4.5rem]">
        {/* The run: a baseline with quarter ticks. */}
        <div aria-hidden className="absolute inset-x-0 bottom-0 h-px bg-foreground/70" />
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <span
            key={t}
            aria-hidden
            className="absolute bottom-0 h-2 w-px -translate-x-1/2 bg-foreground/50"
            style={{ left: `${t * 100}%` }}
          />
        ))}
        {/* Shock windows: a tinted span in the creative's colour. */}
        {ordered
          .filter((s) => s.kind === "shock")
          .map((s) => (
            <span
              key={`w-${s.key}`}
              aria-hidden
              className="absolute bottom-0 h-7"
              style={{
                left: `${s.atFrac * 100}%`,
                width: `${((s.untilFrac ?? 1) - s.atFrac) * 100}%`,
                backgroundColor: `color-mix(in srgb, ${color(s.creativeId, shifts.indexOf(s))} 16%, transparent)`,
                borderTop: `2px solid ${color(s.creativeId, shifts.indexOf(s))}`,
              }}
            />
          ))}
        {ordered.map((s, i) => {
          const c = color(s.creativeId, shifts.indexOf(s));
          const label = `Shift ${i + 1}: ${KIND_LABELS[s.kind].toLowerCase()}`;
          return (
            <div key={s.key}>
              <Pin
                frac={s.atFrac}
                n={i + 1}
                color={c}
                tag={s.kind === "shock" ? "Starts" : KIND_LABELS[s.kind].split(" ")[0]}
                focused={focus === `${s.key}:at`}
              />
              <input
                type="range"
                min={0}
                max={100}
                step={1}
                value={Math.round(s.atFrac * 100)}
                disabled={disabled}
                aria-label={`${label}, starts`}
                aria-valuetext={`${pct(s.atFrac)} of the run`}
                onFocus={() => setFocus(`${s.key}:at`)}
                onBlur={() => setFocus(null)}
                onChange={(e) => onMove(s.key, "atFrac", Number(e.target.value) / 100)}
                className={cn("pointer-events-none absolute -inset-x-3.5 bottom-0 h-16 appearance-none bg-transparent opacity-0", THUMB)}
              />
              {s.kind === "shock" && (
                <>
                  <Pin frac={s.untilFrac ?? 1} color={c} tag="Ends" small focused={focus === `${s.key}:until`} />
                  <input
                    type="range"
                    min={0}
                    max={100}
                    step={1}
                    value={Math.round((s.untilFrac ?? 1) * 100)}
                    disabled={disabled}
                    aria-label={`${label}, ends`}
                    aria-valuetext={`${pct(s.untilFrac ?? 1)} of the run`}
                    onFocus={() => setFocus(`${s.key}:until`)}
                    onBlur={() => setFocus(null)}
                    onChange={(e) => onMove(s.key, "untilFrac", Number(e.target.value) / 100)}
                    className={cn("pointer-events-none absolute -inset-x-3.5 bottom-0 h-16 appearance-none bg-transparent opacity-0", THUMB)}
                  />
                </>
              )}
            </div>
          );
        })}
      </div>
      <div aria-hidden className="relative mx-3.5 mt-1.5 h-4 text-[11px] text-muted-foreground tabular-nums">
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <span
            key={t}
            className={cn("absolute", t === 0 ? "" : t === 1 ? "-translate-x-full" : "-translate-x-1/2")}
            style={{ left: `${t * 100}%` }}
          >
            {t === 0 ? "Start of run" : t === 1 ? "End" : pct(t)}
          </span>
        ))}
      </div>
    </div>
  );
}

/** A pin: a stem from the baseline and a round head (numbered for a start), in the creative's colour. */
function Pin({
  frac,
  n,
  color,
  tag,
  small = false,
  focused,
}: {
  frac: number;
  n?: number;
  color: string;
  tag: string;
  small?: boolean;
  focused: boolean;
}) {
  return (
    <div aria-hidden className="pointer-events-none absolute bottom-0 -translate-x-1/2" style={{ left: `${frac * 100}%` }}>
      <div className="flex flex-col items-center">
        <span
          className={cn(
            "flex items-center justify-center rounded-full font-semibold text-white tabular-nums ring-2 ring-card",
            small ? "size-3" : "size-6 text-xs",
            focused && "outline-3 outline-offset-2 outline-ring/60"
          )}
          style={{ backgroundColor: color }}
        >
          {n ?? ""}
        </span>
        <span className={cn("w-0.5", small ? "h-6" : "h-9")} style={{ backgroundColor: color }} />
      </div>
      <span
        className={cn(
          "absolute left-1/2 ml-2 text-[11px] whitespace-nowrap text-muted-foreground",
          small ? "top-[-0.1rem]" : "top-0.5"
        )}
      >
        {tag} {pct(frac)}
      </span>
    </div>
  );
}

// ── Per-event form ───────────────────────────────────────────────────────────

function ShiftForm({
  n,
  shift: s,
  ctx,
  color,
  sentence,
  errors,
  disabled,
  onChange,
  onRemove,
  onEarlier,
  onLater,
}: {
  n: number;
  shift: EditorShift;
  ctx: ShiftContext;
  color: string;
  sentence: string;
  errors: ShiftError[];
  disabled: boolean;
  onChange: (s: EditorShift) => void;
  onRemove: () => void;
  onEarlier?: () => void;
  onLater?: () => void;
}) {
  const uid = useId();
  const b = shiftBounds(ctx.scenario, ctx.ctrMode);
  const segs = scenarioSegments(ctx.scenario);
  const leaderOk = s.kind === "demote" || s.kind === "shock";
  return (
    <li className="grid gap-3 px-3.5 py-3 sm:grid-cols-[1.5rem_minmax(0,1fr)_auto]">
      <span
        aria-hidden
        className="hidden size-6 items-center justify-center rounded-full text-xs font-semibold text-white tabular-nums sm:flex"
        style={{ backgroundColor: color }}
      >
        {n}
      </span>
      <div className="min-w-0">
        <p className="text-base leading-snug text-foreground">
          <span className="sr-only">Shift {n}: </span>
          {sentence}
        </p>
        <div className="mt-2.5 flex flex-wrap items-end gap-x-5 gap-y-3">
          <Field label="What changes" htmlFor={`${uid}-kind`}>
            <select
              id={`${uid}-kind`}
              value={s.kind}
              disabled={disabled}
              onChange={(e) => onChange(changeKind(s, e.target.value as ShiftKind, ctx))}
              className="h-8 rounded-sm border border-input bg-card px-2 text-sm disabled:opacity-50"
            >
              {SHIFT_KINDS.map((k) => (
                <option key={k} value={k}>
                  {KIND_LABELS[k]}
                </option>
              ))}
            </select>
          </Field>

          {s.kind !== "mix" && (
            <Field label="Readers" htmlFor={`${uid}-seg`}>
              <select
                id={`${uid}-seg`}
                value={s.segment ?? ""}
                disabled={disabled}
                onChange={(e) => onChange({ ...s, segment: e.target.value || null })}
                className="h-8 rounded-sm border border-input bg-card px-2 text-sm disabled:opacity-50"
              >
                <option value="">Everyone</option>
                {segs.map((g) => (
                  <option key={g} value={g}>
                    {segmentPhrase(g).replace(/^\w/, (c) => c.toUpperCase())}
                  </option>
                ))}
              </select>
            </Field>
          )}

          {s.kind !== "mix" && (
            <fieldset className="min-w-0">
              <legend className="mb-1 text-xs text-muted-foreground">Creative</legend>
              <div className="flex flex-wrap gap-1">
                {leaderOk && (
                  <CreativeChip
                    active={s.creativeId === SHIFT_LEADER}
                    color={INK}
                    hollow
                    disabled={disabled}
                    onClick={() => onChange({ ...s, creativeId: SHIFT_LEADER })}
                  >
                    Current leader
                  </CreativeChip>
                )}
                {ctx.creatives.map((c) => (
                  <CreativeChip
                    key={c.creativeId}
                    active={s.creativeId === c.creativeId}
                    color={c.color}
                    disabled={disabled}
                    onClick={() => onChange({ ...s, creativeId: c.creativeId })}
                  >
                    {c.name}
                  </CreativeChip>
                ))}
              </div>
            </fieldset>
          )}
        </div>

        <div className="mt-3 grid max-w-xl gap-x-6 gap-y-3 sm:grid-cols-2">
          {s.kind === "promote" && (
            <Magnitude
              label="Lead over the next best"
              value={s.liftPp ?? b.liftPp[0]}
              bounds={b.liftPp}
              format={(v) => `+${formatPts(v)}`}
              disabled={disabled}
              onChange={(v) => onChange({ ...s, liftPp: v })}
            />
          )}
          {s.kind === "demote" && (
            <Magnitude
              label="Drop below the next best"
              value={s.dropPp ?? b.dropPp[0]}
              bounds={b.dropPp}
              format={(v) => `−${formatPts(v)}`}
              disabled={disabled}
              onChange={(v) => onChange({ ...s, dropPp: v })}
            />
          )}
          {s.kind === "shock" && (
            <>
              <Magnitude
                label="Click multiplier"
                value={s.ctrMultiplier ?? 1}
                bounds={b.ctrMultiplier}
                step={0.05}
                format={(v) => `×${Number(v.toFixed(2))}`}
                marks={[1]}
                disabled={disabled}
                onChange={(v) => onChange({ ...s, ctrMultiplier: v })}
              />
              <div>
                <div className="grid grid-cols-2 gap-3">
                  <Magnitude
                    label="Starts"
                    value={s.atFrac}
                    bounds={b.atFrac}
                    step={0.01}
                    format={pct}
                    disabled={disabled}
                    onChange={(v) => onChange(moveShift(s, v))}
                  />
                  <Magnitude
                    label="Ends"
                    value={s.untilFrac ?? 1}
                    bounds={b.untilFrac}
                    step={0.01}
                    format={pct}
                    disabled={disabled}
                    onChange={(v) => onChange({ ...s, untilFrac: Math.max(v, s.atFrac + SHIFT_MIN_WINDOW) })}
                  />
                </div>
              </div>
            </>
          )}
          {s.kind === "mix" && <MixSliders shift={s} ctx={ctx} disabled={disabled} onChange={onChange} />}
        </div>
        {errors.map((e) => (
          <p key={e.field} role="alert" className="mt-2 text-sm text-mark-fail">
            {e.message}
          </p>
        ))}
      </div>
      <div className="flex items-start gap-0.5 sm:flex-col sm:items-end">
        <Button variant="ghost" size="icon-sm" disabled={disabled} onClick={onRemove} aria-label={`Remove shift ${n}`}>
          <XIcon />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={disabled || !onEarlier}
          onClick={onEarlier}
          aria-label={`Move shift ${n} earlier`}
          title="Swap with the shift before"
        >
          <ChevronUpIcon />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={disabled || !onLater}
          onClick={onLater}
          aria-label={`Move shift ${n} later`}
          title="Swap with the shift after"
        >
          <ChevronDownIcon />
        </Button>
      </div>
    </li>
  );
}

function Field({ label, htmlFor, children }: { label: string; htmlFor: string; children: ReactNode }) {
  return (
    <div>
      <FieldLabel as="label" htmlFor={htmlFor} className="mb-1 block">
        {label}
      </FieldLabel>
      {children}
    </div>
  );
}

function CreativeChip({
  active,
  color,
  hollow = false,
  disabled,
  onClick,
  children,
}: {
  active: boolean;
  color: string;
  hollow?: boolean;
  disabled: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "inline-flex h-8 max-w-[14rem] items-center gap-1.5 rounded-sm border px-2 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50",
        active ? "border-foreground bg-foreground/[0.06] font-medium text-foreground" : "border-input bg-card text-foreground/80 hover:bg-muted"
      )}
    >
      <span
        aria-hidden
        className="size-2.5 shrink-0 rounded-full"
        style={hollow ? { boxShadow: `inset 0 0 0 1.5px ${color}` } : { backgroundColor: color }}
      />
      <span className="truncate">{children}</span>
    </button>
  );
}

function Magnitude({
  label,
  value,
  bounds,
  step,
  format,
  marks,
  disabled,
  onChange,
}: {
  label: string;
  value: number;
  bounds: readonly [number, number];
  step?: number;
  format: (v: number) => string;
  marks?: number[];
  disabled: boolean;
  onChange: (v: number) => void;
}) {
  const id = useId();
  const st = step ?? Number(((bounds[1] - bounds[0]) / 50).toPrecision(2));
  return (
    <div className="min-w-0">
      <div className="flex items-baseline justify-between gap-2">
        <FieldLabel id={id}>{label}</FieldLabel>
        <span className="text-sm text-foreground tabular-nums">{format(value)}</span>
      </div>
      <Slider
        value={value}
        min={bounds[0]}
        max={bounds[1]}
        step={st}
        marks={marks}
        disabled={disabled}
        aria-labelledby={id}
        valueText={format}
        onValueChange={(v) => onChange(Math.round(v * 1e4) / 1e4)}
      />
    </div>
  );
}

function MixSliders({
  shift: s,
  ctx,
  disabled,
  onChange,
}: {
  shift: EditorShift;
  ctx: ShiftContext;
  disabled: boolean;
  onChange: (s: EditorShift) => void;
}) {
  const segs = scenarioSegments(ctx.scenario);
  const floor = shiftBounds(ctx.scenario, ctx.ctrMode).segmentMix[0];
  const mix = s.segmentMix?.length === segs.length ? s.segmentMix : segs.map(() => 1 / segs.length);
  const total = mix.reduce((a, b) => a + b, 0) || 1;
  const norm = mix.map((w) => w / total);
  const shown = displayPercents(norm);
  return (
    <>
      {segs.map((g, i) => (
        <Magnitude
          key={g}
          label={segmentPhrase(g).replace(/^\w/, (c) => c.toUpperCase())}
          value={norm[i]}
          bounds={[floor, 1 - floor * (segs.length - 1)]}
          step={0.01}
          format={() => `${shown[i]}% of readers`}
          disabled={disabled}
          onChange={(v) => onChange({ ...s, segmentMix: rebalanceMix(norm, i, v, floor).map((w) => Math.round(w * 1e4) / 1e4) })}
        />
      ))}
    </>
  );
}

// ── Before / after preview ───────────────────────────────────────────────────

function PreviewStrip({ pv, ctx }: { pv: NonNullable<ReturnType<typeof shiftPreview>>; ctx: ShiftContext }) {
  const all = pv.regimes.flatMap((g) => g.ctr.flat());
  const range: [number, number] = [Math.min(...all), Math.max(...all)];
  const digits = ctx.ctrMode === "realistic" ? 2 : 1;
  return (
    <div className="mt-5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <span className="flex items-center gap-1">
          <h4 className="text-sm font-semibold text-foreground">What readers will want, period by period</h4>
          <InfoTip label="About the shift preview" align="start">
            {SHIFT_HELP.preview}
          </InfoTip>
        </span>
        <span className="text-xs text-muted-foreground">Expected click rate by segment, before random variation</span>
      </div>
      <div className="mt-2 flex gap-3 overflow-x-auto pb-1">
        {pv.regimes.map((g, gi) => {
          const prev = gi > 0 ? pv.regimes[gi - 1] : null;
          return (
            <figure key={`${g.start}`} className="shrink-0 rounded-md border border-border p-2">
              <figcaption className="mb-1.5 flex items-baseline justify-between gap-3 px-0.5">
                <span className="text-sm font-semibold text-foreground">{pv.labels[gi]}</span>
                <span className="text-[11px] text-muted-foreground tabular-nums">
                  {pct(g.start / ctx.horizon)}–{pct(g.end / ctx.horizon)}
                </span>
              </figcaption>
              <table className="border-separate border-spacing-[3px] text-sm">
                <caption className="sr-only">
                  {pv.labels[gi]}: expected click rate of each creative with each segment; the best creative per segment is
                  outlined.
                </caption>
                <thead>
                  <tr>
                    <th scope="col" className="sr-only">
                      Creative
                    </th>
                    {pv.segments.map((sg) => (
                      <th
                        key={sg}
                        scope="col"
                        title={segmentPhrase(sg)}
                        className="max-w-[3.75rem] truncate px-0.5 text-center text-[11px] font-normal text-muted-foreground"
                      >
                        {segmentPhrase(sg).split(" ")[0]}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {ctx.creatives.map((c, a) => (
                    <tr key={c.creativeId}>
                      <th scope="row" className="pr-1">
                        <span className="sr-only">{c.name}</span>
                        <span aria-hidden className="block size-2.5 rounded-full" style={{ backgroundColor: c.color }} />
                      </th>
                      {pv.segments.map((sg, si) => {
                        const v = g.ctr[a][si];
                        const best = g.oracle[si] === a;
                        const moved = prev ? Math.abs(v - prev.ctr[a][si]) > 5e-4 : false;
                        return (
                          <td key={sg} className="p-0">
                            <div
                              title={`${c.name}, ${segmentPhrase(sg)}: ${formatPercent(v, 2)}`}
                              className={cn(
                                CONDENSED,
                                "flex h-8 w-[3.75rem] items-center justify-center rounded-[3px] text-[0.95rem] leading-none tabular-nums",
                                moved ? "text-foreground" : "text-foreground/70"
                              )}
                              style={{
                                backgroundColor: `rgb(26 29 33 / ${(SHADE_MIN + cellShade(v, range) * SHADE_SPAN).toFixed(3)})`,
                                boxShadow: best ? `inset 0 0 0 2px ${c.color}` : undefined,
                              }}
                            >
                              {formatPercent(v, digits)}
                            </div>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </figure>
          );
        })}
      </div>
      <p className="mt-1.5 text-xs text-muted-foreground">
        Outlined in its colour: the best creative for the segment in that period. Shocks and swaps resolve in time order,
        on top of earlier shifts.
      </p>
    </div>
  );
}
