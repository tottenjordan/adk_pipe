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
import { segmentWords, skewedSegment } from "./scenario-preview";

/** Below this many episodes the bands are too loose to call a winner. */
export const MIN_EPISODES = 5;

/** A share change smaller than this (3 points) counts as "no real movement". */
const SHARE_MOVE_MIN = 0.03;

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
}

export interface InsightInput {
  metrics: ExperimentMetrics | null | undefined;
  series?: CreativeSeries | null;
  arms: Arm[];
  rewardMode?: string;
  ctrMode?: string;
  scenario?: string;
  scenarioOverrides?: ScenarioOverrides | null;
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

const baselinesOf = (policies: string[]) => policies.filter((p) => p !== LIN && p !== ORACLE);

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
  k: number
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
    return { verdict: "empty", headline: "", detail: "", support: "", notes, readings: { ...EMPTY_READINGS }, lanes: {} };
  }
  const units = unitsFor(input.rewardMode);
  const facts = creativeFacts(input);
  const k = Math.max(arms.length, facts.length);
  const { headline, detail } = headlineFor(verdict, metrics, facts, units, k);
  const hasSegments = Object.keys(metrics.perSegment ?? {}).length > 0;
  return {
    verdict,
    headline,
    detail,
    support: supportFor(verdict, metrics, input.ctrMode),
    notes,
    readings: {
      avgReward: avgRewardReading(metrics, units),
      regret: regretReading(metrics, units),
      optimal: optimalReading(metrics),
      share: shareReading(facts, k),
      segments: segmentsReading(metrics, nameLookup(arms)),
      totals: totalsReading(metrics, units, verdict),
    },
    lanes: Object.fromEntries(facts.map((f) => [f.id, laneReading(f, units, hasSegments)])),
  };
}
