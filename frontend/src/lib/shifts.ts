/**
 * Scripted behaviour shifts (docs/bandit/contracts.md §10): the shift editor's
 * model (defaults, presets, validation, the REST payload, one plain-language
 * sentence per event), plus tolerant readers for the per-run payloads the api
 * adds (traffic runs, shift response, regimes) and the run selector's URL state.
 *
 * Bounds and kinds come from `scenario-presets.generated.json` (bandit.config),
 * never hand-copied. Every reader degrades to "nothing" on older APIs: no runs,
 * no shift response, no regimes means the page renders exactly as before.
 */
import { formatInt, formatPercent } from "./chart";
import type { CtrMode, Scenario, Shift, ShiftKind, TrafficRun } from "./experiments";
import {
  applyShifts,
  displayPercents,
  formatPts,
  previewMatrix,
  rebalanceMix,
  SCENARIO_PRESETS,
  SHIFT_LEADER,
  SHIFT_SPEC,
  shiftRound,
  type ArmScores,
  type ShiftPreview,
  type TuneValues,
} from "./scenario-preview";

export { SHIFT_LEADER, SHIFT_SPEC };
export const SHIFT_KINDS: readonly ShiftKind[] = SHIFT_SPEC.kinds;
export const MAX_SHIFTS = SHIFT_SPEC.maxShifts;
export const SHIFT_MIN_WINDOW = SHIFT_SPEC.minWindow;

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const round4 = (v: number) => Math.round(v * 1e4) / 1e4;
const EPS = 1e-9;

// ── Context ──────────────────────────────────────────────────────────────────

export interface ShiftCreative {
  creativeId: string;
  name: string;
  color: string;
  scores: ArmScores;
}

/** What the editor needs to know about the experiment. */
export interface ShiftContext {
  scenario: Scenario;
  ctrMode: CtrMode;
  horizon: number;
  /** In arm order (the order the experiment page colours them by). */
  creatives: ShiftCreative[];
  /** The experiment's reader settings (preset values when untuned). */
  values: TuneValues;
}

/** The scenario's segment names (empty for an unknown scenario). */
export function scenarioSegments(scenario: string): string[] {
  return SCENARIO_PRESETS[scenario as Scenario]?.segments.map((s) => s.name) ?? [];
}

/** `ctr_scale(ctr_mode)`: realistic magnitudes are this fraction of demo ones. */
export function ctrScale(scenario: string, ctrMode: string): number {
  const sc = SCENARIO_PRESETS[scenario as Scenario];
  if (!sc) return 1;
  const t = sc.targetCtr[ctrMode as CtrMode];
  return finite(t) ? t / sc.targetCtr.demo : 1;
}

export type BoundField = keyof typeof SHIFT_SPEC.bounds;

/** Inclusive bounds per field; liftPp / dropPp scaled to the click-rate mode. */
export function shiftBounds(scenario: string, ctrMode: string): Record<BoundField, [number, number]> {
  const s = ctrScale(scenario, ctrMode);
  const b = SHIFT_SPEC.bounds;
  return {
    ...b,
    liftPp: [b.liftPp[0] * s, b.liftPp[1] * s],
    dropPp: [b.dropPp[0] * s, b.dropPp[1] * s],
  };
}

// ── Editor model ─────────────────────────────────────────────────────────────

/** A shift in the editor: the REST fields plus a stable key for React. */
export type EditorShift = Shift & { key: string };

let keySeq = 0;
const nextKey = () => `s${++keySeq}`;

const clamp = (v: number, [lo, hi]: readonly [number, number]) => Math.min(hi, Math.max(lo, v));

/** The scenario mix (or the tuned one): one share per segment, summing to 1. */
function baseMix(ctx: ShiftContext): number[] {
  const segs = SCENARIO_PRESETS[ctx.scenario]?.segments ?? [];
  const v = ctx.values.segmentMix;
  if (v.length === segs.length) {
    const t = v.reduce((a, b) => a + b, 0);
    return v.map((w) => w / t);
  }
  return segs.map((s) => s.weight);
}

/** The segment whose readers are most often on a phone (the "mobile surge" preset). */
export function mobileSegment(scenario: string): number {
  const segs = SCENARIO_PRESETS[scenario as Scenario]?.segments ?? [];
  let best = 0;
  let bestP = -1;
  segs.forEach((s, i) => {
    const dev = s.marginals.devicetype;
    const p = typeof dev === "object" && dev ? Number(dev.mobile ?? 0) / Object.values(dev).reduce((a, b) => a + Number(b), 0) : 0;
    if (p > bestP + EPS) {
      best = i;
      bestP = p;
    }
  });
  return best;
}

function preview(ctx: ShiftContext) {
  if (ctx.creatives.length < 2 || !(ctx.scenario in SCENARIO_PRESETS)) return null;
  return previewMatrix({
    scenario: ctx.scenario,
    ctrMode: ctx.ctrMode,
    arms: ctx.creatives.map((c) => c.scores),
    values: ctx.values,
  });
}

/** A time not already taken by another event, near `want`. */
function freeTime(want: number, taken: number[]): number {
  const [lo, hi] = SHIFT_SPEC.bounds.atFrac;
  for (const d of [0, 0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.05, -0.05]) {
    const t = round4(clamp(want + d, [lo, hi]));
    if (taken.every((x) => Math.abs(x - t) > 0.02)) return t;
  }
  return round4(clamp(want, [lo, hi]));
}

/** A new event of `kind` with sensible, visible defaults, placed clear of the others. */
export function newShift(kind: ShiftKind, ctx: ShiftContext, existing: readonly Shift[] = []): EditorShift {
  const b = shiftBounds(ctx.scenario, ctx.ctrMode);
  const s = ctrScale(ctx.scenario, ctx.ctrMode);
  const taken = existing.map((x) => x.atFrac);
  const key = nextKey();
  switch (kind) {
    case "promote": {
      const pv = preview(ctx);
      // A challenger: the creative with the lowest expected click rate overall.
      const weakest = pv ? pv.overall.indexOf(Math.min(...pv.overall)) : ctx.creatives.length - 1;
      return {
        key,
        kind,
        atFrac: freeTime(0.4, taken),
        segment: null,
        creativeId: ctx.creatives[Math.max(0, weakest)]?.creativeId ?? "",
        liftPp: round4(clamp(0.015 * s, b.liftPp)),
      };
    }
    case "demote":
      return {
        key,
        kind,
        atFrac: freeTime(0.5, taken),
        segment: null,
        creativeId: SHIFT_LEADER,
        dropPp: round4(clamp(0.015 * s, b.dropPp)),
      };
    case "mix": {
      const mix = baseMix(ctx);
      const i = mobileSegment(ctx.scenario);
      return {
        key,
        kind,
        atFrac: freeTime(0.3, taken),
        segmentMix: rebalanceMix(mix, i, 0.6, b.segmentMix[0]).map(round4),
      };
    }
    case "shock": {
      const at = freeTime(0.6, taken);
      return {
        key,
        kind,
        atFrac: at,
        untilFrac: round4(Math.min(at + 0.15, SHIFT_SPEC.bounds.untilFrac[1])),
        segment: null,
        creativeId: SHIFT_LEADER,
        ctrMultiplier: 0.6,
      };
    }
  }
}

export type ShiftPresetId = "demote-leader-halfway" | "mobile-surge" | "leader-fatigue";

export const SHIFT_PRESETS: { id: ShiftPresetId; label: string; description: string }[] = [
  {
    id: "demote-leader-halfway",
    label: "Demote the leader at halfway",
    description: "Readers go off the leading creative where it wins.",
  },
  { id: "mobile-surge", label: "Mobile surge at 30%", description: "Phone readers become 60% of the audience." },
  {
    id: "leader-fatigue",
    label: "Ad fatigue on the leader 60–75%",
    description: "The leader gets 40% fewer clicks for a while, then recovers.",
  },
];

/**
 * One preset event. "Demote the leader" is resolved against the preview so it
 * always shows: where the leader wins exactly one segment (segment-specific
 * winners), it is demoted for that segment by name; otherwise for everyone.
 */
export function presetShift(id: ShiftPresetId, ctx: ShiftContext, existing: readonly Shift[] = []): EditorShift {
  const b = shiftBounds(ctx.scenario, ctx.ctrMode);
  const s = ctrScale(ctx.scenario, ctx.ctrMode);
  const key = nextKey();
  const free = (t: number) => (existing.some((x) => Math.abs(x.atFrac - t) < EPS) ? freeTime(t, existing.map((x) => x.atFrac)) : t);
  if (id === "demote-leader-halfway") {
    const base: EditorShift = {
      key,
      kind: "demote",
      atFrac: free(0.5),
      segment: null,
      creativeId: SHIFT_LEADER,
      dropPp: round4(clamp(0.02 * s, b.dropPp)),
    };
    const pv = preview(ctx);
    if (!pv) return base;
    const leader = pv.overall.indexOf(Math.max(...pv.overall));
    const won = pv.oracle.map((w, i) => (w === leader ? i : -1)).filter((i) => i >= 0);
    if (won.length === 1 && pv.segments.length > 1 && new Set(pv.oracle).size > 1) {
      return { ...base, segment: pv.segments[won[0]], creativeId: ctx.creatives[leader].creativeId };
    }
    return base;
  }
  if (id === "mobile-surge") {
    return { ...newShift("mix", ctx, existing), key, atFrac: free(0.3) };
  }
  const at = free(0.6);
  return {
    key,
    kind: "shock",
    atFrac: at,
    untilFrac: round4(Math.min(at + 0.15, 1)),
    segment: null,
    creativeId: SHIFT_LEADER,
    ctrMultiplier: 0.6,
  };
}

/** Change an event's kind, keeping its time and segment where they still apply. */
export function changeKind(sh: EditorShift, kind: ShiftKind, ctx: ShiftContext): EditorShift {
  if (sh.kind === kind) return sh;
  const fresh = newShift(kind, ctx);
  const out: EditorShift = { ...fresh, key: sh.key, atFrac: sh.atFrac };
  if (kind === "shock") out.untilFrac = round4(Math.min(sh.atFrac + 0.15, 1));
  if (kind !== "mix" && sh.kind !== "mix") out.segment = sh.segment ?? null;
  if (kind === "promote" && out.creativeId === SHIFT_LEADER) out.creativeId = ctx.creatives[0]?.creativeId;
  return out;
}

/**
 * Move an event to `atFrac`, keeping a shock's length (clamped so the window
 * stays inside the run and at least SHIFT_MIN_WINDOW long).
 */
export function moveShift(sh: EditorShift, atFrac: number): EditorShift {
  const at = round4(clamp(atFrac, SHIFT_SPEC.bounds.atFrac));
  if (sh.kind !== "shock") return { ...sh, atFrac: at };
  const len = Math.max((sh.untilFrac ?? at + 0.1) - sh.atFrac, SHIFT_MIN_WINDOW);
  const until = round4(clamp(at + len, SHIFT_SPEC.bounds.untilFrac));
  return { ...sh, atFrac: round4(Math.min(at, until - SHIFT_MIN_WINDOW)), untilFrac: until };
}

/** Swap two events' places in time ("move earlier / later"); each keeps its own settings. */
export function swapTimes(list: readonly EditorShift[], i: number, j: number): EditorShift[] {
  if (i < 0 || j < 0 || i >= list.length || j >= list.length || i === j) return [...list];
  const out = [...list];
  out[i] = moveShift(list[i], list[j].atFrac);
  out[j] = moveShift(list[j], list[i].atFrac);
  return out;
}

/** Events in time order (stable on atFrac, ties keep list order): the order they resolve in. */
export function timeOrder<T extends Pick<Shift, "atFrac">>(list: readonly T[]): T[] {
  return list
    .map((s, i) => ({ s, i }))
    .sort((a, b) => a.s.atFrac - b.s.atFrac || a.i - b.i)
    .map((x) => x.s);
}

// ── Validation (mirrors bandit.config.validate_shifts) ───────────────────────

export interface ShiftError {
  /** Position in the list; -1 for the list as a whole. */
  index: number;
  /** camelCase field, as the api's `detail.field` names it. */
  field: string;
  message: string;
}

const pctText = (v: number) => `${Math.round(v * 100)}%`;

/** Every problem the api would reject, in plain words. [] when the script is valid. */
export function validateShifts(list: readonly Shift[], ctx: ShiftContext): ShiftError[] {
  const errs: ShiftError[] = [];
  if (list.length > MAX_SHIFTS) errs.push({ index: -1, field: "shifts", message: `At most ${MAX_SHIFTS} shifts per run.` });
  const b = shiftBounds(ctx.scenario, ctx.ctrMode);
  const segs = scenarioSegments(ctx.scenario);
  const ids = new Set(ctx.creatives.map((c) => c.creativeId));
  const within = (v: unknown, [lo, hi]: readonly [number, number]) => finite(v) && v >= lo - EPS && v <= hi + EPS;
  list.forEach((s, i) => {
    const add = (field: string, message: string) => errs.push({ index: i, field, message });
    if (!SHIFT_KINDS.includes(s.kind)) {
      add("kind", "Pick what changes.");
      return;
    }
    if (!within(s.atFrac, b.atFrac)) {
      add("atFrac", `Start between ${pctText(b.atFrac[0])} and ${pctText(b.atFrac[1])} of the run.`);
    }
    if (s.kind === "mix") {
      const mix = s.segmentMix ?? [];
      if (mix.length !== segs.length) add("segmentMix", `Give one share per segment (${segs.length}).`);
      else if (!mix.every((w) => within(w, b.segmentMix))) {
        add("segmentMix", `Every segment keeps at least ${pctText(b.segmentMix[0])} of readers.`);
      }
      return;
    }
    if (s.segment !== undefined && s.segment !== null && !segs.includes(s.segment)) {
      add("segment", "Pick one of this scenario's segments, or everyone.");
    }
    const leaderOk = SHIFT_SPEC.leaderKinds.includes(s.kind);
    if (!(s.creativeId && (ids.has(s.creativeId) || (leaderOk && s.creativeId === SHIFT_LEADER)))) {
      add("creativeId", leaderOk ? "Pick a creative or the current leader." : "Pick a creative.");
    }
    if (s.kind === "promote" && !within(s.liftPp, b.liftPp)) {
      add("liftPp", `Lead by ${formatPts(b.liftPp[0])} to ${formatPts(b.liftPp[1])}.`);
    }
    if (s.kind === "demote" && !within(s.dropPp, b.dropPp)) {
      add("dropPp", `Drop by ${formatPts(b.dropPp[0])} to ${formatPts(b.dropPp[1])}.`);
    }
    if (s.kind === "shock") {
      if (!within(s.ctrMultiplier, b.ctrMultiplier)) {
        add("ctrMultiplier", `Multiply clicks by ${b.ctrMultiplier[0]}× to ${b.ctrMultiplier[1]}×.`);
      }
      if (!within(s.untilFrac, b.untilFrac)) {
        add("untilFrac", `End between ${pctText(b.untilFrac[0])} and ${pctText(b.untilFrac[1])} of the run.`);
      } else if (finite(s.atFrac) && (s.untilFrac as number) < s.atFrac + SHIFT_MIN_WINDOW - EPS) {
        add("untilFrac", `A shock lasts at least ${pctText(SHIFT_MIN_WINDOW)} of the run.`);
      }
    }
  });
  return errs;
}

// ── Payload ──────────────────────────────────────────────────────────────────

/** Editor events → the camelCase REST list (§10): only each kind's fields, rounded. */
export function shiftsToPayload(list: readonly Shift[]): Shift[] {
  return list.map((s) => {
    const base = { kind: s.kind, atFrac: round4(s.atFrac) };
    switch (s.kind) {
      case "promote":
        return { ...base, segment: s.segment ?? null, creativeId: s.creativeId, liftPp: round4(s.liftPp ?? 0) };
      case "demote":
        return { ...base, segment: s.segment ?? null, creativeId: s.creativeId, dropPp: round4(s.dropPp ?? 0) };
      case "mix":
        return { ...base, segmentMix: (s.segmentMix ?? []).map(round4) };
      case "shock":
        return {
          ...base,
          untilFrac: round4(s.untilFrac ?? 1),
          segment: s.segment ?? null,
          creativeId: s.creativeId,
          ctrMultiplier: round4(s.ctrMultiplier ?? 1),
        };
    }
  });
}

export interface TrafficBody {
  episodes: number;
  horizon?: number;
  shifts?: Shift[];
  forget?: boolean;
}

/**
 * The `POST …/traffic` body. Without shifts it is exactly what older APIs
 * accept (`{episodes, horizon?}`); with shifts it adds `shifts` and `forget`.
 */
export function trafficBody(episodes: number, horizon?: number, shifts: readonly Shift[] = [], forget?: boolean): TrafficBody {
  const body: TrafficBody = horizon ? { episodes, horizon } : { episodes };
  if (shifts.length) {
    body.shifts = shiftsToPayload(shifts);
    body.forget = forget ?? true;
  }
  return body;
}

/** The forgetting toggle's default: on when there is at least one shift. */
export function defaultForget(count: number): boolean {
  return count > 0;
}

// ── Plain-language sentences ─────────────────────────────────────────────────

export function segmentPhrase(segment: string | null | undefined): string {
  return segment ? segment.replace(/[_-]+/g, " ").trim().toLowerCase() : "everyone";
}


/** "At 40% of the run" / "From 60% to 75% of the run". */
function whenPhrase(s: Shift): string {
  return s.kind === "shock"
    ? `From ${pctText(s.atFrac)} to ${pctText(s.untilFrac ?? 1)} of the run`
    : `At ${pctText(s.atFrac)} of the run`;
}

/**
 * One sentence per event, e.g. "At 40% of the run, mobile scrollers start
 * preferring The Jackpot Reveal (+1.5 pts)." `leaderName` names who "the
 * leader" resolves to in the preview, when known.
 */
export function describeShift(
  s: Shift,
  ctx: Pick<ShiftContext, "creatives" | "scenario" | "values">,
  leaderName?: string | null
): string {
  const name = (id: string | undefined) =>
    id === SHIFT_LEADER
      ? leaderName
        ? `the leader at that point (${leaderName})`
        : "whichever creative leads at that point"
      : (ctx.creatives.find((c) => c.creativeId === id)?.name ?? "a creative");
  // "mobile scrollers" reads as a group already; "late night casual" needs a noun.
  const phrase = segmentPhrase(s.segment);
  const who = s.segment && !phrase.endsWith("s") ? `${phrase} readers` : phrase;
  const when = whenPhrase(s);
  switch (s.kind) {
    case "promote":
      return `${when}, ${who} start${s.segment ? "" : "s"} preferring ${name(s.creativeId)} (+${formatPts(s.liftPp ?? 0)} over the next best).`;
    case "demote":
      return `${when}, ${who} go${s.segment ? "" : "es"} off ${name(s.creativeId)}: it falls ${formatPts(s.dropPp ?? 0)} below the next best.`;
    case "mix": {
      const segs = scenarioSegments(ctx.scenario);
      const mix = s.segmentMix ?? [];
      if (mix.length !== segs.length) return `${when}, the audience mix changes.`;
      const total = mix.reduce((a, b) => a + b, 0) || 1;
      const now = mix.map((w) => w / total);
      const before = (() => {
        const v = ctx.values.segmentMix;
        const t = v.reduce((a, b) => a + b, 0) || 1;
        return v.length === segs.length ? v.map((w) => w / t) : segs.map(() => 1 / segs.length);
      })();
      const i = now.map((w, k) => w - before[k]).reduce((best, d, k, arr) => (d > arr[best] ? k : best), 0);
      const pNow = displayPercents(now)[i];
      const pBefore = displayPercents(before)[i];
      return `${when}, ${segmentPhrase(segs[i])} ${pNow >= pBefore ? "surge" : "fall"} to ${pNow}% of readers (from ${pBefore}%).`;
    }
    case "shock": {
      const m = s.ctrMultiplier ?? 1;
      const change = m < 1 ? `${Math.round((1 - m) * 100)}% fewer clicks` : `${Math.round((m - 1) * 100)}% more clicks`;
      return `${when}, ${name(s.creativeId)} gets ${change}${s.segment ? ` from ${who}` : ""}, then recovers.`;
    }
  }
}

/** Short kind label for a pin and a form heading. */
export const KIND_LABELS: Record<ShiftKind, string> = {
  promote: "Promote a challenger",
  demote: "Demote a creative",
  mix: "Audience mix",
  shock: "Temporary shock",
};

// ── Live preview ─────────────────────────────────────────────────────────────

export interface ShiftPreviewResult extends ShiftPreview {
  segments: string[];
  /** The resolved creative's name per requested index ("leader" resolved). */
  resolvedNames: Record<number, string>;
  /** A label per regime: "Before", "After shift 1", "During shift 2", … */
  labels: string[];
}

/** Before/after preview for the editor (null with < 2 creatives or no valid shifts). */
export function shiftPreview(ctx: ShiftContext, list: readonly Shift[]): ShiftPreviewResult | null {
  const pv = preview(ctx);
  if (!pv) return null;
  const valid = list.filter((_, i) => !validateShifts([list[i]], ctx).length);
  const sp = applyShifts(pv, valid, { horizon: ctx.horizon, armIds: ctx.creatives.map((c) => c.creativeId) });
  const resolvedNames: Record<number, string> = {};
  for (const r of sp.resolved) {
    if (r.creativeIndex !== null) resolvedNames[list.indexOf(valid[r.index])] = ctx.creatives[r.creativeIndex]?.name ?? "";
  }
  const marks = sp.resolved.map((r) => ({ round: r.round, endRound: r.endRound, kind: r.kind }));
  return { ...sp, segments: pv.segments, resolvedNames, labels: regimeLabels(sp.regimes, marks) };
}

// ── Run shifts (the per-run record) ──────────────────────────────────────────

/** One shift of a traffic run, normalised from requested or resolved, camel or snake. */
export interface RunShift {
  kind: ShiftKind;
  atFrac: number;
  untilFrac: number | null;
  segment: string | null;
  /** As requested: an arm id, or "leader" (demote and shock). */
  creativeId: string | null;
  /**
   * For "leader": the creative the shift hit. The traffic job's own resolution
   * (`metrics.resolvedShifts`) when the run has it; for older runs, inferred
   * from the run's metrics regimes (highest true click rate in the period that
   * ends at the shift's round); null when unknown.
   */
  resolvedCreativeId?: string | null;
  requestedCreativeId: string | null;
  liftPp: number | null;
  dropPp: number | null;
  ctrMultiplier: number | null;
  segmentMix: number[] | null;
  /** 0-based first shifted round (from the record, else round(atFrac · T)). */
  round: number;
  endRound: number | null;
}

const num = (v: unknown): number | null => (finite(v) ? v : null);

/**
 * Read one shift from a traffic run's record (contracts §5: the validated REST
 * form, camelCase). Rounds follow §10: round(atFrac · T), and a shock ends at
 * round(untilFrac · T). Null when it isn't a shift.
 */
export function normalizeShift(raw: unknown, horizon: number | null): RunShift | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Partial<Record<keyof Shift, unknown>>;
  const kind = o.kind as ShiftKind;
  const at = num(o.atFrac);
  if (!SHIFT_KINDS.includes(kind) || at === null) return null;
  const until = num(o.untilFrac);
  const creative = typeof o.creativeId === "string" ? o.creativeId : null;
  const mix = o.segmentMix;
  return {
    kind,
    atFrac: at,
    untilFrac: until,
    segment: typeof o.segment === "string" ? o.segment : null,
    creativeId: creative,
    requestedCreativeId: creative,
    liftPp: num(o.liftPp),
    dropPp: num(o.dropPp),
    ctrMultiplier: num(o.ctrMultiplier),
    segmentMix: Array.isArray(mix) && mix.every(finite) ? (mix as number[]) : null,
    round: horizon ? shiftRound(at, horizon) : 0,
    endRound: kind === "shock" && horizon ? shiftRound(until ?? 1, horizon) : null,
  };
}

/** A run's shifts, in time order. */
export function runShifts(run: Pick<TrafficRun, "shifts" | "horizon"> | null | undefined, horizon?: number | null): RunShift[] {
  const h = run?.horizon ?? horizon ?? null;
  const list = (Array.isArray(run?.shifts) ? run.shifts : [])
    .map((s) => normalizeShift(s, h))
    .filter((s): s is RunShift => s !== null);
  return timeOrder(list).sort((a, b) => a.round - b.round);
}

// ── Markers + regimes ────────────────────────────────────────────────────────

export interface ChartMarker {
  x: number;
  label: string;
}

/**
 * Vertical markers for the charts: one at each shift's first round ("Shift 1"),
 * plus "Shift N ends" at a shock's end when it ends inside the run. x is the
 * 1-based round count reached when the shift starts, matching checkpoint
 * semantics (checkpoint r covers exactly the pre-shift rounds).
 */
export function shiftMarkers(shifts: readonly Pick<RunShift, "round" | "endRound" | "kind">[], horizon?: number | null): ChartMarker[] {
  const out: ChartMarker[] = [];
  shifts.forEach((s, i) => {
    out.push({ x: s.round, label: `Shift ${i + 1}` });
    if (s.kind === "shock" && s.endRound !== null && (!horizon || s.endRound < horizon)) {
      out.push({ x: s.endRound, label: `Shift ${i + 1} ends` });
    }
  });
  return out.sort((a, b) => a.x - b.x);
}

/** "Before", "After shift 1", "During shift 2", "After shift 2 ends" for regimes split at shift rounds. */
export function regimeLabels(
  regimes: readonly { start: number }[],
  shifts: readonly Pick<RunShift, "round" | "endRound" | "kind">[]
): string[] {
  return regimes.map((g) => {
    if (g.start <= 0) return "Before";
    const startIdx = shifts.findIndex((s) => s.round === g.start);
    if (startIdx >= 0) return `${shifts[startIdx].kind === "shock" ? "During" : "After"} shift ${startIdx + 1}`;
    const endIdx = shifts.findIndex((s) => s.endRound === g.start);
    if (endIdx >= 0) return `After shift ${endIdx + 1} ends`;
    return `From round ${formatInt(g.start)}`;
  });
}

export interface MetricRegime {
  start: number;
  end: number;
  perSegment: Record<string, { optimalArm: string; policies: Record<string, { pctOptimal: number; avgReward: number }> }>;
  /** Mean true click rate per creative in the regime (from `arms[]`). */
  trueCtr: Record<string, number>;
}

type RawPerSegment = Record<string, { optimalArm?: unknown; policies?: Record<string, { pctOptimal?: unknown; avgReward?: unknown }> }>;

/**
 * `metrics.regimes` (contracts §5/§10: `[{start, end, perSegment, arms: [{creativeId,
 * trueCtr}]}]`), dropping malformed entries; [] on older APIs or runs without shifts.
 */
export function metricRegimes(raw: unknown): MetricRegime[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((r): MetricRegime | null => {
      if (!r || typeof r !== "object") return null;
      const o = r as { start?: unknown; end?: unknown; perSegment?: unknown; arms?: unknown };
      const start = num(o.start);
      const end = num(o.end);
      if (start === null || end === null) return null;
      const perSegment: MetricRegime["perSegment"] = {};
      for (const [seg, v] of Object.entries((o.perSegment ?? {}) as RawPerSegment)) {
        if (typeof v?.optimalArm !== "string") continue;
        const pols: Record<string, { pctOptimal: number; avgReward: number }> = {};
        for (const [p, pv] of Object.entries(v.policies ?? {})) {
          const pct = num(pv?.pctOptimal);
          if (pct !== null) pols[p] = { pctOptimal: pct, avgReward: num(pv?.avgReward) ?? 0 };
        }
        perSegment[seg] = { optimalArm: v.optimalArm, policies: pols };
      }
      const trueCtr: Record<string, number> = {};
      for (const a of Array.isArray(o.arms) ? (o.arms as { creativeId?: unknown; trueCtr?: unknown }[]) : []) {
        if (typeof a?.creativeId === "string" && finite(a.trueCtr)) trueCtr[a.creativeId] = a.trueCtr;
      }
      return { start, end, perSegment, trueCtr };
    })
    .filter((r): r is MetricRegime => r !== null);
}

/** One shift as the traffic job resolved it (contracts §5/§10 `metrics.resolvedShifts`). */
export interface ResolvedShift {
  /** Position in the requested list. */
  index: number;
  kind: ShiftKind;
  round: number;
  endRound: number | null;
  segment: string | null;
  /** Concrete creative ("leader" resolved); null for a mix shift. */
  creativeId: string | null;
  requestedCreativeId: string | null;
  targets: { segment: string; ctrBefore: number | null; ctrAfter: number | null }[];
}

/**
 * `metrics.resolvedShifts` (time order), dropping malformed entries; [] on older
 * APIs or runs recorded before the traffic job wrote them.
 */
export function parseResolvedShifts(raw: unknown): ResolvedShift[] {
  if (!Array.isArray(raw)) return [];
  const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
  return raw
    .map((r): ResolvedShift | null => {
      if (!r || typeof r !== "object") return null;
      const o = r as Record<string, unknown>;
      const kind = o.kind as ShiftKind;
      const round = num(o.round);
      if (!SHIFT_KINDS.includes(kind) || round === null) return null;
      const targets = (Array.isArray(o.targets) ? (o.targets as Record<string, unknown>[]) : [])
        .filter((t) => typeof t?.segment === "string")
        .map((t) => ({ segment: t.segment as string, ctrBefore: num(t.ctrBefore), ctrAfter: num(t.ctrAfter) }));
      return {
        index: num(o.index) ?? 0,
        kind,
        round,
        endRound: num(o.endRound),
        segment: str(o.segment),
        creativeId: str(o.creativeId),
        requestedCreativeId: str(o.requestedCreativeId),
        targets,
      };
    })
    .filter((r): r is ResolvedShift => r !== null);
}

export interface SeriesRegime {
  start: number;
  end: number;
  /** Segment → optimal creative id in this regime (`segmentWinners`). */
  optimal: Record<string, string>;
  creatives: { creativeId: string; ctr: number | null; trueCtr: number | null; impressions: number | null }[];
  /** Per creative × segment numbers in this regime (from `creatives[].segments[]`). */
  cells: { creativeId: string; segment: string; ctr: number | null; impressions: number }[];
}

type RawSeriesCreative = {
  creativeId?: unknown;
  impressions?: unknown;
  clicks?: unknown;
  ctr?: unknown;
  trueCtr?: unknown;
  segments?: { segment?: unknown; impressions?: unknown; clicks?: unknown; ctr?: unknown }[];
};

/**
 * `series.regimes` (contracts §8/§10: `[{index, start, end, impressions,
 * segmentWinners, creatives: [{creativeId, impressions, clicks, share, ctr,
 * trueCtr, segmentsWon, segments[]}]}]`); [] on older APIs or runs without shifts.
 */
export function seriesRegimes(raw: unknown): SeriesRegime[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((r): SeriesRegime | null => {
      if (!r || typeof r !== "object") return null;
      const o = r as { start?: unknown; end?: unknown; segmentWinners?: unknown; creatives?: unknown };
      const start = num(o.start);
      const end = num(o.end);
      if (start === null || end === null) return null;
      const optimal: Record<string, string> = {};
      for (const [seg, id] of Object.entries((o.segmentWinners ?? {}) as Record<string, unknown>)) {
        if (typeof id === "string") optimal[seg] = id;
      }
      const raws = (Array.isArray(o.creatives) ? (o.creatives as RawSeriesCreative[]) : []).filter(
        (c): c is RawSeriesCreative & { creativeId: string } => typeof c?.creativeId === "string"
      );
      const rate = (ctr: unknown, clicks: unknown, imps: number | null) =>
        num(ctr) ?? (finite(clicks) && imps ? clicks / imps : null);
      const creatives = raws.map((c) => {
        const imps = num(c.impressions);
        return { creativeId: c.creativeId, ctr: rate(c.ctr, c.clicks, imps), trueCtr: num(c.trueCtr), impressions: imps };
      });
      const cells = raws.flatMap((c) =>
        (c.segments ?? [])
          .filter((s) => typeof s?.segment === "string")
          .map((s) => {
            const imps = num(s.impressions) ?? 0;
            return { creativeId: c.creativeId, segment: s.segment as string, ctr: rate(s.ctr, s.clicks, imps), impressions: imps };
          })
      );
      return { start, end, optimal, creatives, cells };
    })
    .filter((r): r is SeriesRegime => r !== null);
}

/** Everything the experiment page needs about the shown run's shifts. */
export interface RunView {
  shifts: RunShift[];
  forget: boolean;
  horizon: number | null;
  markers: ChartMarker[];
  metricRegimes: MetricRegime[];
  seriesRegimes: SeriesRegime[];
  /** The regimes the page splits by (series regimes when present, else metrics regimes). */
  regimes: { start: number; end: number }[];
  /** One label per entry of `regimes`. */
  labels: string[];
}

/**
 * The shown run's shift view, or null when the run has no shifts (older APIs,
 * or a run without a script): the page then renders exactly as before.
 */
export function buildRunView(
  run: Pick<TrafficRun, "shifts" | "horizon" | "forget"> | null | undefined,
  metrics: { horizon?: number | null; regimes?: unknown; resolvedShifts?: unknown } | null | undefined,
  series: { regimes?: unknown } | null | undefined
): RunView | null {
  const horizon = run?.horizon ?? metrics?.horizon ?? null;
  const shifts = runShifts(run, horizon);
  if (!shifts.length) return null;
  const metricR = metricRegimes(metrics?.regimes);
  const seriesR = seriesRegimes(series?.regimes);
  const regimes = seriesR.length ? seriesR : metricR;
  // The traffic job's resolution, in the same time order; used only when it
  // lines up with the recorded script (same count, kind and round per shift).
  const resolved = parseResolvedShifts(metrics?.resolvedShifts);
  const aligned =
    resolved.length === shifts.length &&
    resolved.every((r, i) => r.kind === shifts[i].kind && (!horizon || r.round === shifts[i].round));
  shifts.forEach((s, i) => {
    if (s.creativeId !== SHIFT_LEADER) return;
    if (aligned && resolved[i].creativeId) {
      s.resolvedCreativeId = resolved[i].creativeId;
      return;
    }
    const prev = metricR.find((g) => g.end === s.round);
    const ranked = Object.entries(prev?.trueCtr ?? {}).sort((a, b) => b[1] - a[1]);
    s.resolvedCreativeId = ranked[0]?.[0] ?? null;
  });
  return {
    shifts,
    forget: Boolean(run?.forget),
    horizon,
    markers: shiftMarkers(shifts, horizon),
    metricRegimes: metricR,
    seriesRegimes: seriesR,
    regimes,
    labels: regimeLabels(regimes, shifts),
  };
}

// ── Shift response (per shift, per policy) ───────────────────────────────────

/** Mean ± 95% interval across episodes (contracts §5 `Stat`), plus the episode count. */
export interface Stat {
  mean: number;
  lo: number | null;
  hi: number | null;
  n: number | null;
}

export interface PolicyShiftResponse {
  pctOptimalBefore: Stat | null;
  pctOptimalAfter: Stat | null;
  regretRateBefore: Stat | null;
  regretRateAfter: Stat | null;
  /** Over the episodes that recovered (null when none did). */
  recoveryRounds: Stat | null;
  recoveredEpisodes: number | null;
  episodes: number | null;
}

export interface ShiftResult {
  /** Position in time order (0-based). */
  order: number;
  round: number;
  policies: Record<string, PolicyShiftResponse>;
}

/** `{mean, lo, hi}` → Stat (null when malformed). */
export function toStat(v: unknown, n?: number | null): Stat | null {
  if (!v || typeof v !== "object") return null;
  const o = v as { mean?: unknown; lo?: unknown; hi?: unknown };
  const mean = num(o.mean);
  if (mean === null) return null;
  return { mean, lo: num(o.lo), hi: num(o.hi), n: n ?? null };
}

type RawShiftResponse = {
  round?: unknown;
  episodes?: unknown;
  pctOptimalBefore?: unknown;
  pctOptimalAfter?: unknown;
  regretRateBefore?: unknown;
  regretRateAfter?: unknown;
  recoveryRounds?: unknown;
  recoveredEpisodes?: unknown;
};

function toPolicyResponse(o: RawShiftResponse): PolicyShiftResponse {
  const episodes = num(o.episodes);
  const recovered = num(o.recoveredEpisodes);
  return {
    pctOptimalBefore: toStat(o.pctOptimalBefore, episodes),
    pctOptimalAfter: toStat(o.pctOptimalAfter, episodes),
    regretRateBefore: toStat(o.regretRateBefore, episodes),
    regretRateAfter: toStat(o.regretRateAfter, episodes),
    recoveryRounds: toStat(o.recoveryRounds, recovered),
    recoveredEpisodes: recovered,
    episodes,
  };
}

/**
 * `metrics.shiftResponse` (contracts §5/§10: policy → one entry per shift in
 * time order), regrouped per shift. Sorted by round; [] when absent ({} without shifts).
 */
export function parseShiftResponse(raw: unknown): ShiftResult[] {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  const out = new Map<number, ShiftResult>();
  for (const [p, list] of Object.entries(raw as Record<string, unknown>)) {
    if (!Array.isArray(list)) continue;
    (list as RawShiftResponse[]).forEach((e, j) => {
      const round = num(e?.round);
      if (round === null) return;
      const r = out.get(j) ?? { order: j, round, policies: {} };
      out.set(j, r);
      r.policies[p] = toPolicyResponse(e);
    });
  }
  return [...out.values()].sort((a, b) => a.round - b.round).map((r, i) => ({ ...r, order: i }));
}

/** The paired cost of the shifts (contracts §5 `shiftCost`): ghost − endpoint, per episode. */
export interface ShiftCost {
  episodes: number | null;
  clicksPerEpisode: Stat | null;
  rewardPerEpisode: Stat | null;
}

/** `metrics.shiftCost`, or null when absent. */
export function parseShiftCost(raw: unknown): ShiftCost | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as { episodes?: unknown; clicksPerEpisode?: unknown; rewardPerEpisode?: unknown };
  const n = num(o.episodes);
  const clicks = toStat(o.clicksPerEpisode, n);
  const reward = toStat(o.rewardPerEpisode, n);
  return clicks || reward ? { episodes: n, clicksPerEpisode: clicks, rewardPerEpisode: reward } : null;
}

/**
 * Shaded recovery spans for the regret chart: from each shift's round for the
 * endpoint's mean recovery time ("Recovery"), or to the end of the run when it
 * never recovered. [] without shift response numbers.
 */
export function recoverySpans(
  results: readonly ShiftResult[],
  horizon: number | null,
  policy = "linear_ts"
): { x0: number; x1: number; label: string }[] {
  return results
    .map((r) => {
      const p = r.policies[policy];
      if (!p?.pctOptimalBefore) return null;
      const rec = p.recoveryRounds?.mean;
      if (finite(rec)) return { x0: r.round, x1: r.round + rec, label: "Back to 80%" };
      return horizon ? { x0: r.round, x1: horizon, label: "Not recovered" } : null;
    })
    .filter((s): s is { x0: number; x1: number; label: string } => s !== null);
}

// ── Run selector ─────────────────────────────────────────────────────────────

/** `?run=` value → a run number (integer ≥ 1), or null (latest). */
export function parseRunParam(value: string | null | undefined): number | null {
  const v = (value ?? "").trim();
  return /^[1-9]\d{0,5}$/.test(v) ? Number(v) : null;
}

/** The current URL with `run` set (null drops it, meaning the latest run), other params kept. */
export function urlForRun(href: string, run: number | null): string {
  const url = new URL(href);
  if (run) url.searchParams.set("run", String(run));
  else url.searchParams.delete("run");
  return `${url.pathname}${url.search}${url.hash}`;
}

/** Runs sorted by number (absent / malformed → []). */
export function trafficRuns(runs: unknown): TrafficRun[] {
  if (!Array.isArray(runs)) return [];
  return (runs as TrafficRun[])
    .filter((r) => r && Number.isInteger(r.run) && r.run >= 1)
    .sort((a, b) => a.run - b.run);
}

/** The run to show: the requested one when it exists, else the latest; null with no runs. */
export function selectedRun(runs: readonly TrafficRun[], requested: number | null): TrafficRun | null {
  if (!runs.length) return null;
  return runs.find((r) => r.run === requested) ?? runs[runs.length - 1];
}

/** "Run 2 of 3 · 2 shifts · forgetting on" (shifts and forgetting omitted when there are none). */
export function runLabel(run: Pick<TrafficRun, "run" | "shifts" | "forget">, total: number): string {
  const n = Array.isArray(run.shifts) ? run.shifts.length : 0;
  const parts = [`Run ${run.run} of ${total}`];
  parts.push(n ? `${n} ${n === 1 ? "shift" : "shifts"}` : "no shifts");
  if (n || run.forget) parts.push(`forgetting ${run.forget ? "on" : "off"}`);
  return parts.join(" · ");
}

/** "50% of the run" for marker hover text. */
export function fracText(frac: number): string {
  return `${formatPercent(frac)} of the run`;
}
