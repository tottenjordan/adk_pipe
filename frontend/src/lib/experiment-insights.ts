/**
 * Plain-language interpretation of a bandit experiment's results: the headline
 * sentence on the Overview, a one-line reading under each Analysis chart, and a
 * short reading per creative lane. Pure and deterministic.
 *
 * Honesty rules:
 * - Every number comes from the payload, formatted with the shared formatters.
 *   A reading whose inputs are missing or non-finite is `null`, never "NaN%".
 * - The endpoint (linear_ts) is only called ahead or behind when its 95% band at
 *   the last checkpoint clears the best baseline's and there are at least
 *   MIN_EPISODES episodes; otherwise the copy says it is too early to call.
 * - When the endpoint trails a baseline, say why in plain words (`why`) rather
 *   than leave the reader to guess: drift (the best and worst creatives swap and
 *   a full-memory endpoint adapts slowly) or the cost of learning reader context.
 */
import { formatCompact, formatInt, formatPercent } from "./chart";
import {
  armName,
  policyShortLabel,
  segmentLabel,
  shortId,
  type Arm,
  type Band,
  type CreativeSeries,
  type ExperimentMetrics,
  type ScenarioOverrides,
} from "./experiments";
import { GHOST_POLICY, isReferencePolicy } from "./experiments";
import { TRAILING_EXPLAIN } from "./experiment-explain";
import { segmentWords, skewedSegment } from "./scenario-preview";
import {
  metricRegimes,
  parseShiftCost,
  parseShiftResponse,
  segmentPhrase,
  SHIFT_LEADER,
  type PolicyShiftResponse,
  type RunShift,
  type ShiftResult,
  type Stat,
} from "./shifts";

/** Below this many episodes the bands are too loose to call a winner. */
export const MIN_EPISODES = 5;

/** A share change smaller than this (3 points) counts as "no real movement". */
const SHARE_MOVE_MIN = 0.03;

/** Reader features the endpoint estimates per creative (ctx-v1, contracts §1). */
const FEATURE_DIM = 19;

/** Readers per endpoint update (the api's batch_size): the discount applies once per batch. */
const BATCH_SIZE = 100;

export type Verdict = "empty" | "too_early" | "ahead" | "behind";

export interface ChartReadings {
  avgReward: string | null;
  regret: string | null;
  optimal: string | null;
  share: string | null;
  segments: string | null;
  totals: string | null;
}

export interface ExperimentInsights {
  verdict: Verdict;
  /** The one-sentence interpretation ("" when there are no results). */
  headline: string;
  /** Where else the endpoint sends traffic (segment winners besides the leader); "" if none. */
  detail: string;
  /** Muted context under the headline: run size, uncertainty, demo caveat. */
  support: string;
  readings: ChartReadings;
  /** One short reading per creative, by creativeId. */
  lanes: Record<string, string>;
  /** Notes on tuned reader settings (misleading judge, skewed mix); [] for preset experiments. */
  notes: string[];
  /** Why the endpoint trails the best baseline ("" when it doesn't, or with no results). */
  why: string;
  /** Explain-mode background for `why` ("" when `why` is empty). */
  whyExplain: string;
  /** One card per scripted shift, in time order ([] for runs without shifts). */
  shiftCards: ShiftCard[];
  /** What the shifts cost against the ghost replay ("" without a ghost). */
  ghost: string;
}

/** One scripted shift's result (contracts §10 `shiftResponse`, the endpoint's numbers). */
export interface ShiftCard {
  key: string;
  /** "Shift 1" … in time order. */
  label: string;
  /** What happened, past tense: "Demoted The Tone Dividend Bailout for late night casual readers". */
  title: string;
  /** "Round 20,000, 50% of the run". */
  when: string;
  /** Endpoint best-creative rate in the window before / after the shift. */
  before: number | null;
  after: number | null;
  status: "too_early" | "held" | "recovered" | "partly" | "not_recovered" | "no_data";
  /** The plain-language reading (no p-values). */
  reading: string;
  /** "10 episodes; ± is a 95% interval across episodes." */
  evidence: string;
}

export interface InsightInput {
  metrics: ExperimentMetrics | null | undefined;
  series?: CreativeSeries | null;
  arms: Arm[];
  rewardMode?: string;
  ctrMode?: string;
  scenario?: string;
  scenarioOverrides?: ScenarioOverrides | null;
  /** The endpoint's per-batch discount γ (1 or missing = full memory). */
  policyDiscount?: number | null;
  /** The shown traffic run's scripted shifts, in time order (contracts §10). */
  shifts?: RunShift[];
  /** Whether that run let the endpoint forget old evidence. */
  forget?: boolean;
}

const LIN = "linear_ts";
const ORACLE = "oracle";

// ── Small pure helpers (exported for tests) ──────────────────────────────────

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** "a", "a and b", "a, b and c". */
export function joinList(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** Signed relative change as a percent of `base`, e.g. 0.056 → "5.6%"; null if undefined. */
export function relativeChange(value: number, base: number): number | null {
  if (!finite(value) || !finite(base) || base === 0) return null;
  return (value - base) / Math.abs(base);
}

/** Last value of a band's series, as {mean, lo, hi} (nulls where missing). */
export function lastOfBand(band: Band | undefined): { mean: number | null; lo: number | null; hi: number | null } {
  const at = (xs: number[] | undefined) => {
    const v = xs?.length ? xs[xs.length - 1] : undefined;
    return finite(v) ? v : null;
  };
  return { mean: at(band?.mean), lo: at(band?.lo), hi: at(band?.hi) };
}

/** Whether two 95% intervals overlap. Missing bounds count as overlapping (unknown). */
export function bandsOverlap(
  a: { lo: number | null; hi: number | null },
  b: { lo: number | null; hi: number | null }
): boolean {
  if (a.lo === null || a.hi === null || b.lo === null || b.hi === null) return true;
  return a.lo <= b.hi && b.lo <= a.hi;
}

const baselinesOf = (policies: string[]) => policies.filter((p) => p !== LIN && p !== ORACLE && !isReferencePolicy(p));

/**
 * The best non-endpoint, non-oracle strategy for a curve at the last checkpoint
 * (highest mean, or lowest with `lowerIsBetter`).
 */
export function bestBaselineOn(
  metrics: ExperimentMetrics,
  key: "cumAvgReward" | "cumRegret" | "pctOptimal",
  lowerIsBetter = false
): string | null {
  let best: string | null = null;
  let bestV = 0;
  for (const p of baselinesOf(Object.keys(metrics.curves ?? {}))) {
    const v = lastOfBand(metrics.curves[p]?.[key]).mean;
    if (v === null) continue;
    if (best === null || (lowerIsBetter ? v < bestV : v > bestV)) {
      best = p;
      bestV = v;
    }
  }
  return best;
}

/** The best baseline by expected total reward, falling back to the reward curve. */
export function bestBaseline(metrics: ExperimentMetrics): string | null {
  let best: string | null = null;
  let bestV = 0;
  for (const p of baselinesOf(Object.keys(metrics.totals ?? {}))) {
    const v = metrics.totals[p]?.mean;
    if (!finite(v)) continue;
    if (best === null || v > bestV) {
      best = p;
      bestV = v;
    }
  }
  return best ?? bestBaselineOn(metrics, "cumAvgReward");
}

/** The endpoint against the best baseline: ahead / behind / too early (bands overlap or few episodes). */
export function verdictOf(metrics: ExperimentMetrics | null | undefined): Verdict {
  if (!metrics || !(metrics.episodes > 0) || !metrics.checkpoints?.length) return "empty";
  const base = bestBaseline(metrics);
  const lin = lastOfBand(metrics.curves?.[LIN]?.cumAvgReward);
  if (!base || lin.mean === null) return "too_early";
  if (metrics.episodes < MIN_EPISODES) return "too_early";
  const b = lastOfBand(metrics.curves?.[base]?.cumAvgReward);
  if (bandsOverlap(lin, b)) return "too_early";
  return (lin.mean ?? 0) > (b.mean ?? 0) ? "ahead" : "behind";
}

// ── Formatting ───────────────────────────────────────────────────────────────

interface Units {
  click: boolean;
  /** "clicks" / "engaged time" — what a strategy earns. */
  earn: string;
  /** "clicks" / "engaged seconds" — counted totals and losses. */
  count: string;
  rate: (v: number) => string;
}

function unitsFor(rewardMode: string | undefined): Units {
  const click = rewardMode !== "engaged";
  return click
    ? { click, earn: "clicks", count: "clicks", rate: (v) => formatPercent(v, 1) }
    : { click, earn: "engaged time", count: "engaged seconds", rate: (v) => `${formatCompact(v)} s` };
}

const pct = (v: number) => formatPercent(v);
const pct1 = (v: number) => formatPercent(v, 1);
const lower = (s: string) => segmentLabel(s).toLowerCase();

function nameLookup(arms: Arm[]) {
  return (id: string) => {
    const arm = arms.find((a) => a.creativeId === id);
    return arm ? armName(arm) : shortId(id);
  };
}

// ── Creative-level facts ─────────────────────────────────────────────────────

interface CreativeFacts {
  id: string;
  name: string;
  first: number | null;
  final: number | null;
  clickRate: number | null;
  trueCtr: number | null;
  segmentsWon: string[];
}

function creativeFacts(input: InsightInput): CreativeFacts[] {
  const { metrics, series, arms } = input;
  const name = nameLookup(arms);
  const won = (id: string) =>
    Object.entries(metrics?.perSegment ?? {})
      .filter(([, s]) => s.optimalArm === id)
      .map(([seg]) => seg)
      .sort();
  if (series?.creatives?.length) {
    return series.creatives.map((c) => ({
      id: c.creativeId,
      name: name(c.creativeId),
      first: finite(c.share?.[0]) ? c.share[0] : null,
      final: finite(c.finalShare) ? c.finalShare : finite(c.share?.at(-1)) ? (c.share.at(-1) as number) : null,
      clickRate: c.impressions > 0 && finite(c.clicks) ? c.clicks / c.impressions : null,
      trueCtr: finite(c.trueCtr) ? c.trueCtr : null,
      segmentsWon: c.segmentsWon?.length ? [...c.segmentsWon].sort() : won(c.creativeId),
    }));
  }
  const ids = new Set([...arms.map((a) => a.creativeId), ...Object.keys(metrics?.armShare ?? {})]);
  return [...ids]
    .map((id) => {
      const share = metrics?.armShare?.[id];
      const row = metrics?.arms?.find((r) => r.creativeId === id);
      const last = share?.length ? share[share.length - 1] : null;
      return {
        id,
        name: name(id),
        first: null,
        final: finite(last) ? last : null,
        clickRate: null,
        trueCtr: finite(row?.trueCtr) ? (row?.trueCtr as number) : null,
        segmentsWon: won(id),
      };
    })
    .sort((a, b) => (b.final ?? -1) - (a.final ?? -1));
}

/** Segments where linear_ts chose the optimal creative more often than an even split. */
function segmentsFound(metrics: ExperimentMetrics, k: number): Set<string> {
  const even = k > 0 ? 1 / k : 0;
  return new Set(
    Object.entries(metrics.perSegment ?? {})
      .filter(([, s]) => {
        const v = s.policies?.[LIN]?.pctOptimal;
        return finite(v) && v > even;
      })
      .map(([seg]) => seg)
  );
}

// ── Headline + support ───────────────────────────────────────────────────────

function headlineFor(
  verdict: Verdict,
  metrics: ExperimentMetrics,
  facts: CreativeFacts[],
  units: Units,
  k: number,
  drift: DriftContext | null
): { headline: string; detail: string } {
  const leader = facts.find((f) => f.final !== null) ?? null;
  const base = bestBaseline(metrics);
  const linTotal = metrics.totals?.[LIN]?.mean;
  const baseTotal = base ? metrics.totals?.[base]?.mean : undefined;
  let rel = finite(linTotal) && finite(baseTotal) ? relativeChange(linTotal, baseTotal) : null;
  if (rel === null && base) {
    const l = lastOfBand(metrics.curves?.[LIN]?.cumAvgReward).mean;
    const b = lastOfBand(metrics.curves?.[base]?.cumAvgReward).mean;
    rel = l !== null && b !== null ? relativeChange(l, b) : null;
  }

  const found = segmentsFound(metrics, k);
  const hasSegments = Object.keys(metrics.perSegment ?? {}).length > 0;

  // Who the endpoint learned to send where (only segments it actually found).
  const others = facts
    .filter((f) => f !== leader)
    .map((f) => ({ f, segs: f.segmentsWon.filter((s) => found.has(s)) }))
    .filter((x) => x.segs.length);
  const routed = others.map(({ f, segs }) => `${joinList(segs.map(lower))} see ${f.name}`).join("; ");
  const detail = routed ? `${routed.charAt(0).toUpperCase()}${routed.slice(1)}.` : "";

  if (verdict === "ahead") {
    const leaderSegs = leader ? leader.segmentsWon.filter((s) => found.has(s)) : [];
    const allSegs = hasSegments && leaderSegs.length === Object.keys(metrics.perSegment).length;
    const favour = leader
      ? `Your endpoint learned to favour ${leader.name}${
          allSegs
            ? " for every reader group"
            : leaderSegs.length
              ? ` for ${joinList(leaderSegs.map(lower))}`
              : ""
        }`
      : "Your endpoint learned which creative to show";
    const gain = rel !== null && rel > 0 ? `, earning ${pct1(rel)} more ${units.earn} than the best baseline strategy.` : ".";
    return { headline: `${favour}${gain}`, detail };
  }

  if (verdict === "behind" && drift?.cause === "shifts" && base) {
    const label = policyShortLabel(base);
    return {
      headline: drift.forgets
        ? `Linear TS trailed ${label} here: your shifts changed what readers want ${drift.when}, and even an endpoint that forgets old evidence has more to re-learn than ${label}.`
        : `Linear TS trailed ${label} here: your shifts changed what readers want ${drift.when}, and an endpoint that weighs old evidence fully adapts slowly.`,
      detail: "",
    };
  }

  if (verdict === "behind" && drift && base) {
    const label = policyShortLabel(base);
    return {
      headline: drift.forgets
        ? `Linear TS trailed ${label} here: the best and worst creatives swap ${drift.when}, and even an endpoint that forgets old evidence has more to re-learn than ${label}.`
        : `Linear TS trailed ${label} here: the best and worst creatives swap ${drift.when}, and an endpoint that weighs old evidence fully adapts slowly.`,
      detail: "",
    };
  }

  if (verdict === "behind") {
    const label = base ? policyShortLabel(base) : "a baseline";
    // Relative to the endpoint: "the baseline earns X% more than the endpoint".
    const baseOverLin = rel !== null && rel < 0 ? -rel / (1 + rel) : null;
    const gap = baseOverLin !== null ? `${pct1(baseOverLin)} more ${units.earn} than` : `more ${units.earn} than`;
    return {
      headline: `The best baseline strategy, ${label}, is earning ${gap} your endpoint so far.`,
      detail: "",
    };
  }

  // too_early
  const lead = leader ? `${leader.name} leads on traffic, but ` : "";
  if (metrics.episodes < MIN_EPISODES) {
    const ep = `${metrics.episodes} ${metrics.episodes === 1 ? "episode" : "episodes"}`;
    return {
      headline: `Too early to call after ${ep}: ${lead}${lead ? "the" : "The"} endpoint needs more episodes to show it beats the baselines.`,
      detail: "",
    };
  }
  return {
    headline: `Too early to call: ${lead}${lead ? "the" : "The"} endpoint and the best baseline are still within each other's margin of error.`,
    detail: "",
  };
}

function supportFor(verdict: Verdict, metrics: ExperimentMetrics, ctrMode?: string): string {
  const ep = `${metrics.episodes} ${metrics.episodes === 1 ? "episode" : "episodes"}`;
  const size = metrics.horizon ? `${ep} of ${formatInt(metrics.horizon)} simulated readers each` : ep;
  const parts = [`${size}, every strategy replayed on the same readers.`];
  if (verdict === "too_early") parts.push("Run more episodes to separate them.");
  if (ctrMode === "demo") parts.push("Demo click rates run high so learning shows quickly: compare creatives with each other, not with live campaigns.");
  return parts.filter(Boolean).join(" ");
}

// ── Why the endpoint trails ──────────────────────────────────────────────────

/** Facts the trailing copy needs when readers' tastes change mid-run (drift, or scripted shifts). */
interface DriftContext {
  forgets: boolean;
  /** "halfway through", "60% of the way through". */
  when: string;
  /** What changed: the drift swap, or the run's scripted shifts. */
  cause: "drift" | "shifts";
  /** Readers the endpoint remembers when it forgets (overrides the discount-derived figure). */
  memory?: number | null;
}

/** When the drift swap happens, from the `driftAtFrac` override (preset: halfway). */
export function swapWhen(driftAtFrac: number | null | undefined): string {
  if (!finite(driftAtFrac) || Math.abs(driftAtFrac - 0.5) < 0.005) return "halfway through";
  return `${pct(driftAtFrac)} of the way through`;
}

/** Whether the endpoint forgets old evidence: a per-batch discount γ in (0, 1). */
export function forgetsEvidence(discount: number | null | undefined): boolean {
  return finite(discount) && discount > 0 && discount < 1;
}

/**
 * Roughly how many readers a discounted endpoint remembers: the effective window
 * N = batch / −ln γ (γ applied once per batch), to one significant figure.
 */
export function memoryReaders(discount: number, batchSize = BATCH_SIZE): number | null {
  if (!forgetsEvidence(discount)) return null;
  const n = batchSize / -Math.log(discount);
  const mag = 10 ** Math.max(0, Math.floor(Math.log10(n)));
  return Math.round(n / mag) * mag;
}

/** How much more the best baseline earns than the endpoint (relative to the endpoint), or null. */
function baselineLead(metrics: ExperimentMetrics, base: string): number | null {
  const lin = metrics.totals?.[LIN]?.mean;
  const b = metrics.totals?.[base]?.mean;
  if (finite(lin) && finite(b)) return lin > 0 && b > lin ? (b - lin) / lin : null;
  const l = lastOfBand(metrics.curves?.[LIN]?.cumAvgReward).mean;
  const c = lastOfBand(metrics.curves?.[base]?.cumAvgReward).mean;
  return l !== null && c !== null && l > 0 && c > l ? (c - l) / l : null;
}

/** One sentence on what lets this baseline adapt or settle faster. */
const BASELINE_EDGE: Record<string, { drift: string; context: string }> = {
  ucb1: {
    drift: "UCB keeps re-checking the creatives it has shown least, so it spots the new winner sooner.",
    context: "UCB only tracks one click rate per creative, so it settles on the winner sooner.",
  },
  beta_bernoulli_ts: {
    drift: "Thompson sampling without context tracks one click rate per creative, so it has far less to re-learn.",
    context: "Thompson sampling without context tracks one click rate per creative, so it settles on the winner sooner.",
  },
  epsilon_greedy: {
    drift: "ε-greedy keeps showing a random creative a fixed share of the time, so it notices the swap sooner.",
    context: "ε-greedy only tracks one click rate per creative, so it settles on the winner sooner.",
  },
};

/**
 * Why the endpoint trails the best baseline, when it does (by mean, whatever the
 * bands say: the headline handles certainty). "" when it leads or ties.
 */
function whyFor(
  metrics: ExperimentMetrics,
  units: Units,
  scenario: string | undefined,
  drift: DriftContext | null,
  discount: number | null | undefined
): { why: string; whyExplain: string } {
  const none = { why: "", whyExplain: "" };
  const base = bestBaseline(metrics);
  if (!base) return none;
  const lead = baselineLead(metrics, base);
  if (lead === null) return none;
  const label = policyShortLabel(base);
  const gap = `${label} earned ${pct1(lead)} more ${units.earn} per episode than your endpoint.`;
  const edge = BASELINE_EDGE[base];
  if (drift) {
    const memory = drift.forgets ? (drift.memory ?? memoryReaders(discount as number)) : null;
    const change = drift.cause === "shifts" ? "each shift" : "the swap";
    const how =
      memory !== null
        ? `This endpoint forgets old evidence on purpose: it weighs each batch of readers a little less than the next, remembering roughly the last ${formatInt(
            memory
          )} readers, so it does recover after ${change}. It still has ${FEATURE_DIM} reader features per creative to re-learn.`
        : `This endpoint weighs every past reader as heavily as the latest one, so after ${change} it keeps backing the old winner until the new evidence outweighs the old.`;
    const setting =
      drift.cause === "shifts"
        ? `In this run your shifts changed what readers want ${drift.when}.`
        : `In this scenario the best and worst creatives swap ${drift.when} the run.`;
    return {
      why: [gap, setting, how, edge?.drift]
        .filter(Boolean)
        .join(" "),
      whyExplain: TRAILING_EXPLAIN.drift,
    };
  }
  const setting =
    scenario === "clear_winner"
      ? `In this scenario one creative is best for every reader, so knowing the reader adds nothing, and the endpoint pays for estimating ${FEATURE_DIM} reader features per creative.`
      : `The endpoint estimates ${FEATURE_DIM} reader features per creative, so it needs more traffic before its reader-by-reader choices pay off.`;
  return {
    why: [gap, setting, edge?.context].filter(Boolean).join(" "),
    whyExplain: TRAILING_EXPLAIN.context,
  };
}

// ── Chart readings ───────────────────────────────────────────────────────────

const overlapNote = " The 95% bands still overlap, so this gap isn't settled.";

function avgRewardReading(metrics: ExperimentMetrics, units: Units): string | null {
  const lin = lastOfBand(metrics.curves?.[LIN]?.cumAvgReward);
  const base = bestBaselineOn(metrics, "cumAvgReward");
  if (lin.mean === null || !base) return null;
  const b = lastOfBand(metrics.curves[base]?.cumAvgReward);
  if (b.mean === null) return null;
  const oracle = lastOfBand(metrics.curves?.[ORACLE]?.cumAvgReward).mean;
  const baseText = `the best baseline (${policyShortLabel(base)}, ${units.rate(b.mean)})`;
  const note = bandsOverlap(lin, b) ? overlapNote : "";
  if (oracle !== null && oracle > b.mean && lin.mean > b.mean) {
    const closed = (lin.mean - b.mean) / (oracle - b.mean);
    return `Linear TS ends at ${units.rate(lin.mean)} against the oracle's ${units.rate(oracle)}, closing ${pct(
      Math.min(closed, 1)
    )} of the gap between ${baseText} and that ceiling.${note}`;
  }
  if (lin.mean <= b.mean) {
    return `Linear TS ends at ${units.rate(lin.mean)}, at or below ${baseText}${
      oracle !== null ? `; the oracle reaches ${units.rate(oracle)}` : ""
    }.${note}`;
  }
  return `Linear TS ends at ${units.rate(lin.mean)} against ${baseText}.${note}`;
}

function regretReading(metrics: ExperimentMetrics, units: Units): string | null {
  const lin = lastOfBand(metrics.curves?.[LIN]?.cumRegret);
  const base = bestBaselineOn(metrics, "cumRegret", true);
  if (lin.mean === null || !base) return null;
  const b = lastOfBand(metrics.curves[base]?.cumRegret);
  if (b.mean === null) return null;
  const rel = relativeChange(lin.mean, b.mean);
  const by = metrics.checkpoints?.length ? ` by round ${formatInt(metrics.checkpoints[metrics.checkpoints.length - 1])}` : "";
  const cmp =
    rel === null
      ? `against ${formatInt(b.mean)} for ${policyShortLabel(base)}`
      : `${pct(Math.abs(rel))} ${rel <= 0 ? "fewer" : "more"} than the best baseline, ${policyShortLabel(base)} (${formatInt(b.mean)})`;
  return `Linear TS lost ${formatInt(lin.mean)} ${units.count} against the oracle${by}, ${cmp}.${
    bandsOverlap(lin, b) ? overlapNote : ""
  }`;
}

function optimalReading(metrics: ExperimentMetrics): string | null {
  const lin = lastOfBand(metrics.curves?.[LIN]?.pctOptimal);
  const base = bestBaselineOn(metrics, "pctOptimal");
  if (lin.mean === null || !base) return null;
  const b = lastOfBand(metrics.curves[base]?.pctOptimal);
  if (b.mean === null) return null;
  const points = Math.round((lin.mean - b.mean) * 100);
  const cmp =
    points === 0
      ? `level with ${policyShortLabel(base)} (${pct(b.mean)})`
      : `${Math.abs(points)} ${Math.abs(points) === 1 ? "point" : "points"} ${points > 0 ? "ahead of" : "behind"} ${policyShortLabel(base)} (${pct(b.mean)})`;
  return `Linear TS showed each reader their best creative ${pct(lin.mean)} of the time by the end, ${cmp}.${
    bandsOverlap(lin, b) ? overlapNote : ""
  }`;
}

function shareReading(facts: CreativeFacts[], k: number): string | null {
  const withFinal = facts.filter((f) => f.final !== null);
  if (!withFinal.length) return null;
  const moves = withFinal.filter((f) => f.first !== null).map((f) => ({ f, d: (f.final as number) - (f.first as number) }));
  if (moves.length >= 2) {
    const up = moves.reduce((a, b) => (b.d > a.d ? b : a));
    const down = moves.reduce((a, b) => (b.d < a.d ? b : a));
    if (Math.max(Math.abs(up.d), Math.abs(down.d)) < SHARE_MOVE_MIN) {
      return "Traffic stayed close to an even split: no creative pulled clearly ahead.";
    }
    const from = (m: typeof up) => `from ${pct(m.f.first as number)} to ${pct(m.f.final as number)}`;
    const toward = up.d >= SHARE_MOVE_MIN ? `toward ${up.f.name}, ${from(up)} of impressions` : "";
    const away = down.d <= -SHARE_MOVE_MIN && down !== up ? `away from ${down.f.name}, ${from(down)}` : "";
    return `Traffic moved ${joinList([toward, away].filter(Boolean))}.`;
  }
  const sorted = [...withFinal].sort((a, b) => (b.final as number) - (a.final as number));
  const top = sorted[0];
  const bottom = sorted[sorted.length - 1];
  const even = k > 0 ? `, against an even split of ${pct(1 / k)}` : "";
  if (top === bottom) return `By the end, ${top.name} gets ${pct(top.final as number)} of impressions${even}.`;
  return `By the end, ${top.name} gets ${pct(top.final as number)} of impressions and ${bottom.name} ${pct(
    bottom.final as number
  )}${even}.`;
}

function segmentsReading(metrics: ExperimentMetrics, name: (id: string) => string): string | null {
  const entries = Object.entries(metrics.perSegment ?? {}).sort(([a], [b]) => a.localeCompare(b));
  if (!entries.length) return null;
  const byArm = new Map<string, string[]>();
  for (const [seg, s] of entries) byArm.set(s.optimalArm, [...(byArm.get(s.optimalArm) ?? []), seg]);
  const wins = [...byArm.entries()]
    .map(([id, segs]) => `${name(id)} wins ${joinList(segs.map(lower))}`)
    .join("; ");
  const led = entries.filter(([, s]) => {
    const lin = s.policies?.[LIN]?.pctOptimal;
    if (!finite(lin)) return false;
    return baselinesOf(Object.keys(s.policies ?? {})).every((p) => {
      const v = s.policies[p]?.pctOptimal;
      return !finite(v) || lin >= v;
    });
  });
  const ledText = led.length
    ? `Linear TS found the winner more often than every baseline in ${led.length} of ${entries.length} ${
        entries.length === 1 ? "segment" : "segments"
      } (${joinList(led.map(([seg]) => lower(seg)))}).`
    : "Linear TS hasn't beaten the best baseline in any segment yet.";
  return `${wins.charAt(0).toUpperCase()}${wins.slice(1)}. ${ledText}`;
}

function totalsReading(metrics: ExperimentMetrics, units: Units, verdict: Verdict): string | null {
  const lin = metrics.totals?.[LIN];
  const base = bestBaseline(metrics);
  const b = base ? metrics.totals?.[base] : undefined;
  if (!lin || !b || !finite(lin.mean) || !finite(b.mean)) return null;
  const sd = (x: { std: number }) => (finite(x.std) ? ` ± ${formatInt(x.std)}` : "");
  const diff = lin.mean - b.mean;
  const rel = relativeChange(lin.mean, b.mean);
  const relText = rel === null ? "" : ` (${diff >= 0 ? "+" : "−"}${pct1(Math.abs(rel))})`;
  const diffText =
    Math.round(diff) === 0 ? "about the same" : `${formatInt(Math.abs(diff))} ${diff > 0 ? "more" : "fewer"}${relText}`;
  const note = verdict === "too_early" ? " That's within the margin of error, so it could still change." : "";
  return `Linear TS collects ${formatInt(lin.mean)}${sd(lin)} ${units.count} per episode against ${formatInt(
    b.mean
  )}${sd(b)} for ${policyShortLabel(base as string)}: ${diffText}.${note}`;
}

// ── Lane readings ────────────────────────────────────────────────────────────

function laneReading(f: CreativeFacts, units: Units, hasSegments: boolean): string {
  const parts: string[] = [];
  if (f.final !== null) {
    let trend = "";
    if (f.first !== null) {
      const d = f.final - f.first;
      trend =
        Math.abs(d) < 0.01
          ? `, about where it started (${pct(f.first)})`
          : `, ${d > 0 ? "up" : "down"} from ${pct(f.first)} at the start`;
    }
    parts.push(`${f.name} ends with ${pct(f.final)} of the endpoint's traffic${trend}.`);
  }
  if (units.click && f.clickRate !== null) {
    parts.push(
      `Readers clicked it ${pct1(f.clickRate)} of the time${
        f.trueCtr !== null ? ` against a true rate of ${pct1(f.trueCtr)}` : ""
      }.`
    );
  } else if (units.click && f.trueCtr !== null) {
    parts.push(`Its true click rate in the simulator is ${pct1(f.trueCtr)}.`);
  }
  if (hasSegments) {
    parts.push(
      f.segmentsWon.length
        ? `It is the best creative for ${joinList(f.segmentsWon.map(lower))}.`
        : "It isn't the best creative for any reader group."
    );
  }
  return parts.join(" ");
}

// ── Entry point ──────────────────────────────────────────────────────────────

const EMPTY_READINGS: ChartReadings = {
  avgReward: null,
  regret: null,
  optimal: null,
  share: null,
  segments: null,
  totals: null,
};

// ── Tuned-reader notes (contracts §9) ────────────────────────────────────────

/**
 * Notes about a custom experiment's reader settings that change how to read its
 * results: a misleading judge (judgeWrong > 0.5) and a skewed audience mix.
 * Empty for preset experiments.
 */
export function setupNotes(scenario: string | undefined, ov: ScenarioOverrides | null | undefined): string[] {
  if (!scenario || !ov) return [];
  const notes: string[] = [];
  if (finite(ov.judgeWrong) && ov.judgeWrong > 0.5) {
    notes.push(
      ov.judgeWrong >= 1
        ? "The eval judge was set to mislead: readers click its top-scored creatives least, so the endpoint had to learn against the scores."
        : "The eval judge was set to mislead: readers lean away from its top-scored creatives, so its scores are a poor guide here."
    );
  }
  const skew = skewedSegment(scenario, ov.segmentMix);
  if (skew) {
    notes.push(
      `Most simulated readers were ${segmentWords(skew.name).toLowerCase()} (${pct(
        skew.share
      )}), so the overall results lean toward what that segment prefers.`
    );
  }
  return notes;
}

/** Interpret one experiment's results. Safe on empty / partial payloads. */
export function buildInsights(input: InsightInput): ExperimentInsights {
  const { metrics, arms } = input;
  const verdict = verdictOf(metrics);
  const notes = setupNotes(input.scenario, input.scenarioOverrides);
  if (verdict === "empty" || !metrics) {
    return {
      verdict: "empty",
      headline: "",
      detail: "",
      support: "",
      notes,
      why: "",
      whyExplain: "",
      readings: { ...EMPTY_READINGS },
      lanes: {},
      shiftCards: [],
      ghost: "",
    };
  }
  const units = unitsFor(input.rewardMode);
  const facts = creativeFacts(input);
  const k = Math.max(arms.length, facts.length);
  const shifts = input.shifts ?? [];
  // With shifts, "best for a segment" means the latest period, never a whole-run blend.
  const periods = shifts.length ? metricRegimes(metrics.regimes) : [];
  const latest = periods.length >= 2 ? periods[periods.length - 1] : null;
  if (latest) {
    for (const f of facts) {
      f.segmentsWon = Object.entries(latest.perSegment)
        .filter(([, v]) => v.optimalArm === f.id)
        .map(([seg]) => seg)
        .sort();
    }
  }
  const drift: DriftContext | null = shifts.length
    ? {
        cause: "shifts",
        forgets: Boolean(input.forget),
        when: shiftsWhen(shifts, metrics.horizon),
        memory: input.forget && metrics.horizon ? roundReaders(metrics.horizon / 8) : null,
      }
    : input.scenario === "drift"
      ? {
          cause: "drift",
          forgets: forgetsEvidence(input.policyDiscount),
          when: swapWhen(input.scenarioOverrides?.driftAtFrac),
        }
      : null;
  const name = nameLookup(arms);
  const results = shifts.length ? parseShiftResponse(metrics.shiftResponse) : [];
  const shiftCards = shifts.length ? buildShiftCards(shifts, results, metrics, name) : [];
  let { headline, detail } = headlineFor(verdict, metrics, facts, units, k, drift);
  if (shifts.length) {
    const sh = shiftHeadline(shiftCards, shifts, metrics, name);
    // The baseline comparison moves into the supporting line, after naming the shifts.
    detail = [sh.detail, verdict === "behind" ? "" : baselineSentence(verdict, metrics, units)].filter(Boolean).join(" ");
    if (verdict !== "behind") headline = sh.headline;
  }
  const { why, whyExplain } = whyFor(metrics, units, input.scenario, drift, input.policyDiscount);
  const hasSegments = Object.keys(metrics.perSegment ?? {}).length > 0;
  return {
    verdict,
    headline,
    detail,
    support: supportFor(verdict, metrics, input.ctrMode),
    notes,
    why,
    whyExplain,
    readings: {
      avgReward: withGhost(avgRewardReading(metrics, units), metrics, units, "avg"),
      regret: withShiftNote(regretReading(metrics, units), shiftCards, "regret"),
      optimal: withShiftNote(optimalReading(metrics), shiftCards, "optimal"),
      share: withShiftNote(shareReading(facts, k), shiftCards, "share"),
      segments: latest
        ? prefixed("In the last period of the run, ", segmentsReading({ ...metrics, perSegment: latest.perSegment }, name))
        : segmentsReading(metrics, name),
      totals: withGhost(totalsReading(metrics, units, verdict), metrics, units, "totals"),
    },
    lanes: Object.fromEntries(facts.map((f) => [f.id, laneReading(f, units, hasSegments)])),
    shiftCards,
    ghost: ghostSentence(metrics, units),
  };
}

// ── Scripted shifts (contracts §10) ──────────────────────────────────────────

const prefixed = (prefix: string, text: string | null) =>
  text ? `${prefix}${text}` : text;

/** Share of the pre-shift best-creative rate that counts as recovered (bandit.metrics.shift_response). */
export const RECOVERY_LEVEL = 0.8;

/** The trailing window recovery is measured over: default_window(T) // 2 (1,000 at the presets). */
export function recoveryFloor(horizon: number | null | undefined): number {
  if (!finite(horizon) || horizon <= 0) return 1000;
  return Math.max(1, Math.floor(Math.min(2000, Math.max(10, Math.floor(horizon / 10))) / 2));
}

/** Two significant figures for round counts: 2634 → 2,600. */
export function roundRounds(v: number): number {
  if (!finite(v) || v <= 0) return 0;
  const mag = 10 ** Math.max(0, Math.floor(Math.log10(v)) - 1);
  return Math.round(v / mag) * mag;
}

function roundReaders(v: number): number {
  const mag = 10 ** Math.max(0, Math.floor(Math.log10(v)));
  return Math.round(v / mag) * mag;
}

/** Half the 95% interval, or null. */
const halfWidth = (st: Stat | null | undefined) =>
  st && st.lo !== null && st.hi !== null ? (st.hi - st.lo) / 2 : null;

/** "at 50% of the run" / "at 50% and 60% of the run". */
function shiftsWhen(shifts: RunShift[], horizon: number | null): string {
  const fr = shifts.map((s) => (horizon ? s.round / horizon : s.atFrac));
  return `at ${joinList([...new Set(fr.map((f) => pct(f)))])} of the run`;
}

/** Who a shift hit: the resolved creative's name, or "the leader at that point". */
function shiftCreative(s: RunShift, name: (id: string) => string): string {
  if (s.creativeId && s.creativeId !== SHIFT_LEADER) return name(s.creativeId);
  return s.resolvedCreativeId ? `the leader at that point (${name(s.resolvedCreativeId)})` : "the leader at that point";
}

/** Past-tense title per shift. */
export function shiftTitle(s: RunShift, name: (id: string) => string): string {
  const seg = segmentPhrase(s.segment);
  const who = s.segment ? `for ${seg}${seg.endsWith("s") ? "" : " readers"}` : "for everyone";
  const c = shiftCreative(s, name);
  switch (s.kind) {
    case "promote":
      return `Promoted ${c} ${who}`;
    case "demote":
      return `Demoted ${c} ${who}`;
    case "mix":
      return "Changed the audience mix";
    case "shock": {
      const m = s.ctrMultiplier ?? 1;
      const by = m < 1 ? `Cut ${c}'s clicks by ${pct(1 - m)}` : `Raised ${c}'s clicks by ${pct(m - 1)}`;
      return `${by}${s.segment ? ` ${who}` : ""}`;
    }
  }
}

const lowerFirst = (t: string) => t.charAt(0).toLowerCase() + t.slice(1);

function cardStatus(r: PolicyShiftResponse | undefined, horizon: number | null): ShiftCard["status"] {
  if (!r || !r.pctOptimalBefore || !r.pctOptimalAfter) return "no_data";
  const n = r.episodes ?? r.pctOptimalBefore.n ?? 0;
  if (n < MIN_EPISODES) return "too_early";
  const before = r.pctOptimalBefore.mean;
  const after = r.pctOptimalAfter.mean;
  const rec = r.recoveryRounds?.mean ?? null;
  const recovered = r.recoveredEpisodes ?? (rec !== null ? n : 0);
  if (after >= RECOVERY_LEVEL * before - 1e-9 && (rec === null || rec <= recoveryFloor(horizon) + 1e-9)) return "held";
  if (recovered <= 0 || rec === null) return "not_recovered";
  return recovered >= n ? "recovered" : "partly";
}

/** One result card per shift, from the endpoint's `shiftResponse` numbers. */
export function buildShiftCards(
  shifts: RunShift[],
  results: ShiftResult[],
  metrics: Pick<ExperimentMetrics, "horizon" | "episodes" | "totals" | "curves">,
  name: (id: string) => string
): ShiftCard[] {
  const base = bestBaseline(metrics as ExperimentMetrics);
  return shifts.map((s, j) => {
    const res = results.find((r) => r.round === s.round) ?? results[j];
    const lin = res?.policies?.[LIN];
    const status = cardStatus(lin, metrics.horizon);
    const n = lin?.episodes ?? lin?.pctOptimalBefore?.n ?? metrics.episodes ?? 0;
    const label = `Shift ${j + 1}`;
    const title = shiftTitle(s, name);
    const when = `Round ${formatInt(s.round)}${metrics.horizon ? `, ${pct(s.round / metrics.horizon)} of the run` : ""}${
      s.kind === "shock" && s.endRound !== null ? `, until round ${formatInt(s.endRound)}` : ""
    }`;
    const before = lin?.pctOptimalBefore?.mean ?? null;
    const after = lin?.pctOptimalAfter?.mean ?? null;
    const epText = `${formatInt(n)} ${n === 1 ? "episode" : "episodes"}`;
    const evidence =
      status === "too_early" || status === "no_data"
        ? `${epText} so far.`
        : `${epText}; ± is a 95% interval across episodes.`;
    const lead = `After you ${lowerFirst(title)} at round ${formatInt(s.round)}`;
    let reading: string;
    if (status === "no_data") {
      reading = "No response numbers for this shift yet.";
    } else if (status === "too_early") {
      reading = `Too early to call after ${epText}: run at least ${MIN_EPISODES} to read how the endpoint responded.`;
    } else {
      const b4 = before as number;
      const af = after as number;
      const fell = `the endpoint's best-creative rate ${af < b4 ? "fell" : "went"} from ${pct(b4)} to ${pct(af)}`;
      const target = `${pct(RECOVERY_LEVEL * b4)} (80% of where it was)`;
      const rec = lin?.recoveryRounds ?? null;
      const hw = halfWidth(rec);
      const recText = rec
        ? `${formatInt(roundRounds(rec.mean))} rounds${hw !== null && hw > 0 ? ` (± ${formatInt(roundRounds(hw))})` : ""}`
        : "";
      const recovered = lin?.recoveredEpisodes ?? n;
      if (status === "held") {
        reading = `${lead}, ${fell}: it stayed above ${target}, so the shift only dented it.`;
      } else if (status === "recovered") {
        reading = `${lead}, ${fell}; it took ${recText} to climb back to ${target}.`;
      } else if (status === "partly") {
        reading = `${lead}, ${fell}; it climbed back to ${target} in ${recovered} of ${n} episodes, after ${recText} on average.`;
      } else {
        reading = `${lead}, ${fell} and never climbed back to ${target} before the run ended.`;
      }
      const b = base ? res?.policies?.[base] : undefined;
      if (base && b?.recoveryRounds && status !== "held" && (b.recoveredEpisodes ?? 1) > 0) {
        reading += ` ${policyShortLabel(base)}, the best baseline, took ${formatInt(roundRounds(b.recoveryRounds.mean))} rounds to get back to 80% of its own rate.`;
      }
    }
    return { key: `${j}-${s.round}`, label, title, when, before, after, status, reading, evidence };
  });
}

/** The Overview headline + supporting line when the run has shifts. */
function shiftHeadline(
  cards: ShiftCard[],
  shifts: RunShift[],
  metrics: ExperimentMetrics,
  name: (id: string) => string
): { headline: string; detail: string } {
  const n = shifts.length;
  const what = joinList(shifts.map((s) => lowerFirst(shiftTitle(s, name))));
  const detail = `You ${what}, ${shiftsWhen(shifts, metrics.horizon)}.`;
  const counted = cards.filter((c) => c.status !== "no_data");
  if (!counted.length) return { headline: `This run had ${n === 1 ? "one shift" : `${n} shifts`}.`, detail };
  if (counted.some((c) => c.status === "too_early")) {
    const ep = `${metrics.episodes} ${metrics.episodes === 1 ? "episode" : "episodes"}`;
    return {
      headline: `Too early to call after ${ep}: run more episodes to see how the endpoint absorbed your ${
        n === 1 ? "shift" : "shifts"
      }.`,
      detail,
    };
  }
  const ok = counted.filter((c) => c.status === "held" || c.status === "recovered");
  const all = n === 2 ? "both shifts" : n === 1 ? "your shift" : `all ${n} shifts`;
  if (ok.length === counted.length) {
    const recs = parseShiftResponse(metrics.shiftResponse)
      .map((r) => r.policies?.[LIN]?.recoveryRounds?.mean)
      .filter(finite);
    const worst = recs.length ? roundRounds(Math.max(...recs)) : null;
    return {
      headline:
        worst !== null && cards.some((c) => c.status === "recovered")
          ? `Your endpoint regained most of its footing after ${all}, within about ${formatInt(worst)} rounds.`
          : `Your endpoint kept most of its footing through ${all}.`,
      detail,
    };
  }
  const missed = counted.length - ok.length;
  return {
    headline: `Your endpoint regained most of its footing after ${ok.length} of ${counted.length} shifts; after the ${
      missed === 1 ? "other" : "others"
    } it stayed below 80% of its earlier best-creative rate.`,
    detail,
  };
}

/** The verdict against the best baseline, as a supporting sentence (shift runs). */
function baselineSentence(verdict: Verdict, metrics: ExperimentMetrics, units: Units): string {
  const base = bestBaseline(metrics);
  if (!base) return "";
  const lin = metrics.totals?.[LIN]?.mean;
  const b = metrics.totals?.[base]?.mean;
  const rel = finite(lin) && finite(b) ? relativeChange(lin, b) : null;
  if (verdict === "ahead" && rel !== null && rel > 0) {
    return `Over the whole run it earned ${pct1(rel)} more ${units.earn} than the best baseline, ${policyShortLabel(base)}.`;
  }
  if (verdict === "too_early") {
    return `Against the best baseline, ${policyShortLabel(base)}, it is still within the margin of error.`;
  }
  return "";
}

/**
 * Ghost minus endpoint reward per episode. Prefers the api's paired
 * `shiftCost` (the per-episode difference, mean ± 95% interval); without it,
 * falls back to the totals with an interval that treats the two as independent
 * (`paired: false`, wider than the truth because both see the same readers).
 */
export function ghostGap(
  metrics: Pick<ExperimentMetrics, "totals" | "episodes" | "shiftCost">,
  click = true
): { diff: number; half: number | null; paired: boolean } | null {
  const cost = parseShiftCost(metrics.shiftCost);
  const st = cost ? (click ? (cost.clicksPerEpisode ?? cost.rewardPerEpisode) : cost.rewardPerEpisode) : null;
  if (st) {
    const half = st.lo !== null && st.hi !== null ? (st.hi - st.lo) / 2 : null;
    return { diff: st.mean, half, paired: true };
  }
  const g = metrics.totals?.[GHOST_POLICY];
  const l = metrics.totals?.[LIN];
  if (!g || !l || !finite(g.mean) || !finite(l.mean)) return null;
  const n = metrics.episodes ?? 0;
  const half =
    finite(g.std) && finite(l.std) && n > 1 ? (1.96 * Math.sqrt(g.std ** 2 + l.std ** 2)) / Math.sqrt(n) : null;
  return { diff: g.mean - l.mean, half, paired: false };
}

function ghostSentence(metrics: ExperimentMetrics, units: Units): string {
  const gg = ghostGap(metrics, units.click);
  if (!gg) return "";
  const interval =
    gg.half === null
      ? ""
      : gg.paired
        ? ` (± ${formatInt(gg.half)})`
        : ` (± ${formatInt(gg.half)}, a cautious interval that treats the two runs as independent)`;
  const amount = `${formatInt(Math.abs(gg.diff))}${interval}`;
  if (Math.round(gg.diff) === 0) {
    return `Without your shifts, the same endpoint would have earned about the same ${units.count} per episode.`;
  }
  return gg.diff > 0
    ? `Without your shifts, the same endpoint would have earned ${amount} more ${units.count} per episode: that is what the shifts cost.`
    : `Without your shifts, the same endpoint would have earned ${amount} fewer ${units.count} per episode: the shifts helped it.`;
}

function withGhost(text: string | null, metrics: ExperimentMetrics, units: Units, where: "avg" | "totals"): string | null {
  if (!text) return text;
  if (where === "avg") {
    const g = lastOfBand(metrics.curves?.[GHOST_POLICY]?.cumAvgReward).mean;
    return g === null ? text : `${text} Without your shifts (dashed) it would have ended at ${units.rate(g)}.`;
  }
  const extra = ghostSentence(metrics, units);
  return extra ? `${text} ${extra}` : text;
}

function withShiftNote(text: string | null, cards: ShiftCard[], where: "regret" | "optimal" | "share"): string | null {
  if (!text || !cards.length) return text;
  if (where === "regret") {
    return `${text} The vertical rules mark your shifts; each shaded stretch runs until the endpoint's best-creative rate was back to 80% of its pre-shift level.`;
  }
  if (where === "share") return `${text} The vertical rules mark your shifts.`;
  const dips = cards
    .filter((c) => c.before !== null && c.after !== null && c.status !== "too_early" && c.status !== "no_data")
    .map((c) => `${c.label.toLowerCase()} took it from ${pct(c.before as number)} to ${pct(c.after as number)}`);
  return dips.length ? `${text} Around the shifts, ${joinList(dips)}.` : text;
}

