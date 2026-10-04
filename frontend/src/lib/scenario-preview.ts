/**
 * Live preview for the Deploy panel's "Advanced: tune the simulated readers"
 * section (docs/bandit/contracts.md §9). Pure and deterministic.
 *
 * `previewMatrix` is a NOISE-FREE port of `bandit/environment.py`
 * `build_true_model` (read that docstring for the model). Click probability for a
 * reader with context x in segment s shown creative a is
 *
 *     sigmoid(α + b_a + u[s, a] + θ_a·x)
 *
 * With the seeded noise switched off, θ_a is non-zero only on four ctx-v1
 * columns (topic_matches_trend, interest_matches_product, devicetype=desktop,
 * devicetype=tablet), so a segment's expected click rate is an exact sum over the
 * 3 × 2 × 2 device/topic/interest combinations weighted by the segment's level
 * probabilities. That replaces the simulator's Monte Carlo context sample with the
 * exact expectation (no sampling error, ~100 sigmoid calls per bisection step),
 * and α is bisected on the exact mixture mean. Parity with the real simulator is
 * pinned by `__tests__/fixtures/scenario-preview-golden.json`
 * (tests/test_scenario_preview_golden.py).
 *
 * Constants come from `scenario-presets.generated.json`
 * (scripts/gen_scenario_presets.py), never hand-copied.
 */
import presetsJson from "./scenario-presets.generated.json";
import type { CtrMode, Scenario, ScenarioOverrides } from "./experiments";
import type { Proof } from "./eval-matching";

// ── Presets ──────────────────────────────────────────────────────────────────

type Marginal = number | Record<string, number>;

export interface SegmentPreset {
  name: string;
  weight: number;
  winnerKey: string;
  marginals: Record<string, Marginal>;
}

export interface ScenarioPreset {
  name: string;
  description: string;
  armEffect: "rank_ctrs" | "segment_winners";
  rankCtrs: number[];
  liftPp: number;
  targetCtr: Record<CtrMode, number>;
  kappa: number;
  lam: number;
  eta: number;
  noiseSd: number;
  thetaSd: number;
  judgeWrong: number;
  drift: { kind: "none" | "abrupt" | "gradual"; atFrac: number; widthFrac: number };
  segments: SegmentPreset[];
}

interface PresetsFile {
  contextSpec: { key: string; levels: string[] }[];
  boolKeys: string[];
  baseMarginals: Record<string, Marginal>;
  /** bandit.config.OVERRIDE_BOUNDS, snake_case keys. */
  overrideBounds: Record<"segment_mix" | "gap_scale" | "judge_wrong" | "noise_scale" | "drift_at_frac", [number, number]>;
  scenarios: Record<Scenario, ScenarioPreset>;
}

const PRESETS = presetsJson as unknown as PresetsFile;

export const SCENARIO_PRESETS: Record<Scenario, ScenarioPreset> = PRESETS.scenarios;

/** Inclusive bounds per knob (bandit.config.OVERRIDE_BOUNDS; segmentMix bounds each raw weight). */
export const OVERRIDE_BOUNDS: Record<keyof TuneValues, readonly [number, number]> = {
  segmentMix: PRESETS.overrideBounds.segment_mix,
  gapScale: PRESETS.overrideBounds.gap_scale,
  judgeWrong: PRESETS.overrideBounds.judge_wrong,
  noiseScale: PRESETS.overrideBounds.noise_scale,
  driftAtFrac: PRESETS.overrideBounds.drift_at_frac,
};

/** Smallest share a segment may keep (each raw weight ≥ this). */
export const MIX_FLOOR = OVERRIDE_BOUNDS.segmentMix[0];

// ── Knob values ──────────────────────────────────────────────────────────────

/** Every knob, always set (the sliders' state). Preset values mean "no override". */
export interface TuneValues {
  /** One share per segment, summing to 1, each ≥ MIX_FLOOR. */
  segmentMix: number[];
  gapScale: number;
  judgeWrong: number;
  noiseScale: number;
  driftAtFrac: number;
}

/** The preset's own values for every knob. */
export function presetValues(scenario: Scenario): TuneValues {
  const p = SCENARIO_PRESETS[scenario];
  return {
    segmentMix: p.segments.map((s) => s.weight),
    gapScale: 1,
    judgeWrong: p.judgeWrong,
    noiseScale: 1,
    driftAtFrac: p.drift.atFrac,
  };
}

const EPS = 1e-6;
const differs = (a: number, b: number) => Math.abs(a - b) > EPS;
const round4 = (v: number) => Math.round(v * 1e4) / 1e4;

/**
 * Only the knobs that differ from the preset, as the camelCase REST object
 * (contracts §9), or undefined when everything is at the preset. `driftAtFrac`
 * is only sent for drift.
 */
export function overridesFromValues(scenario: Scenario, v: TuneValues): ScenarioOverrides | undefined {
  const base = presetValues(scenario);
  const out: ScenarioOverrides = {};
  if (
    v.segmentMix.length === base.segmentMix.length &&
    v.segmentMix.some((w, i) => differs(w, base.segmentMix[i]))
  ) {
    out.segmentMix = v.segmentMix.map(round4);
  }
  if (differs(v.gapScale, base.gapScale)) out.gapScale = round4(v.gapScale);
  if (differs(v.judgeWrong, base.judgeWrong)) out.judgeWrong = round4(v.judgeWrong);
  if (differs(v.noiseScale, base.noiseScale)) out.noiseScale = round4(v.noiseScale);
  if (scenario === "drift" && differs(v.driftAtFrac, base.driftAtFrac)) {
    out.driftAtFrac = round4(v.driftAtFrac);
  }
  return Object.keys(out).length ? out : undefined;
}

/** Overrides (possibly partial or null) → full knob values, filling gaps from the preset. */
export function valuesFromOverrides(scenario: Scenario, ov: ScenarioOverrides | null | undefined): TuneValues {
  const base = presetValues(scenario);
  if (!ov) return base;
  const mix = ov.segmentMix?.length === base.segmentMix.length ? normalise(ov.segmentMix) : base.segmentMix;
  return {
    segmentMix: mix,
    gapScale: ov.gapScale ?? base.gapScale,
    judgeWrong: ov.judgeWrong ?? base.judgeWrong,
    noiseScale: ov.noiseScale ?? base.noiseScale,
    driftAtFrac: ov.driftAtFrac ?? base.driftAtFrac,
  };
}

function normalise(ws: number[]): number[] {
  const total = ws.reduce((a, b) => a + b, 0);
  return total > 0 ? ws.map((w) => w / total) : ws.map(() => 1 / ws.length);
}

/**
 * Set segment `index` to `share` and rebalance the others proportionally to their
 * current shares, keeping every segment ≥ `floor` and the total at 1. `share` is
 * clamped to [floor, 1 − floor·(n−1)].
 */
export function rebalanceMix(mix: number[], index: number, share: number, floor = MIX_FLOOR): number[] {
  const n = mix.length;
  if (n < 2 || index < 0 || index >= n || !Number.isFinite(share)) return [...mix];
  const target = Math.min(Math.max(share, floor), 1 - floor * (n - 1));
  const out = [...mix];
  out[index] = target;
  let free = [...Array(n).keys()].filter((i) => i !== index);
  let rest = 1 - target;
  // Water-fill: share `rest` in proportion to the current weights; pin anyone who
  // would fall below the floor at the floor and redistribute what's left.
  for (let guard = 0; guard < n; guard++) {
    const total = free.reduce((a, i) => a + Math.max(mix[i], 0), 0);
    const share = (i: number) => (total > 0 ? (Math.max(mix[i], 0) / total) * rest : rest / free.length);
    const low = free.filter((i) => share(i) < floor);
    if (low.length === 0) {
      for (const i of free) out[i] = share(i);
      break;
    }
    for (const i of low) out[i] = floor;
    rest -= floor * low.length;
    free = free.filter((i) => !low.includes(i));
    if (free.length === 0) break;
  }
  return out;
}

/** Whole percentages that sum to exactly 100 (largest remainder), for display. */
export function displayPercents(mix: number[]): number[] {
  const raw = mix.map((w) => w * 100);
  const floors = raw.map(Math.floor);
  let left = 100 - floors.reduce((a, b) => a + b, 0);
  const order = raw.map((r, i) => ({ i, frac: r - floors[i] })).sort((a, b) => b.frac - a.frac || a.i - b.i);
  for (const { i } of order) {
    if (left <= 0) break;
    floors[i] += 1;
    left -= 1;
  }
  return floors;
}

// ── Arm scores ───────────────────────────────────────────────────────────────

export type ArmScores = Record<string, number>;

/**
 * The scores the api freezes for a creative (runserver `snapshot_arms` /
 * `_eval_scores`): every judge dimension /10 clamped to [0, 1] (visual
 * dimensions overwrite same-named ad-copy ones), plus `ad_copy_overall` and
 * `visual_overall`.
 */
export function armScoresFromProof(p: Pick<Proof, "adCopyEval" | "visualEval">): ArmScores {
  const out: ArmScores = {};
  const add = (score: { overall_score?: unknown; verdicts?: { dimension?: string; score?: unknown }[] } | undefined, prefix: string) => {
    if (!score) return;
    for (const v of score.verdicts ?? []) {
      if (v.dimension && typeof v.score === "number") out[v.dimension] = Math.max(0, Math.min(1, v.score / 10));
    }
    if (typeof score.overall_score === "number") out[`${prefix}_overall`] = score.overall_score;
  };
  add(p.adCopyEval?.score, "ad_copy");
  add(p.visualEval?.score, "visual");
  return out;
}

/** `ArmSpec.overall`: `overall`, else the mean of ad-copy/visual overall, else the mean of all, else 0.5. */
export function armOverall(scores: ArmScores): number {
  if ("overall" in scores) return scores.overall;
  const parts = ["ad_copy_overall", "visual_overall"].filter((k) => k in scores).map((k) => scores[k]);
  if (parts.length) return parts.reduce((a, b) => a + b, 0) / parts.length;
  const all = Object.values(scores);
  if (!all.length) return 0.5;
  return all.reduce((a, b) => a + b, 0) / all.length;
}

/** `ArmSpec.score(key)`: the dimension, falling back to the overall score. */
export function armScore(scores: ArmScores, key: string): number {
  return key === "overall" ? armOverall(scores) : (scores[key] ?? armOverall(scores));
}

// ── Model maths ──────────────────────────────────────────────────────────────

const logit = (p: number) => Math.log(p) - Math.log1p(-p);
const sigmoid = (z: number) => 1 / (1 + Math.exp(-z));
const mean = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;

/** `apply_scenario_overrides`' gap spread for rank_ctrs: sigmoid(mid + g·(logit(c) − mid)). Stays in (0, 1). */
export function spreadRankCtrs(ctrs: number[], g: number): number[] {
  const ls = ctrs.map(logit);
  const mid = mean(ls);
  return ls.map((z) => sigmoid(mid + g * (z - mid)));
}

/** `_rank_ctrs`: K logits best → worst, interpolating the knots in logit space. */
function rankLogits(knots: number[], k: number, scale: number): number[] {
  const lk = knots.map((c) => logit(c * scale));
  const m = lk.length;
  return Array.from({ length: k }, (_, i) => {
    const x = k === 1 ? 0 : i / (k - 1);
    const pos = x * (m - 1);
    const j = Math.min(Math.floor(pos), m - 2);
    const t = pos - j;
    return lk[j] + t * (lk[j + 1] - lk[j]);
  });
}

/** `_level_probs`: per group, probabilities over its levels for one segment. */
function levelProbs(marginals: Record<string, Marginal>): Record<string, number[]> {
  const out: Record<string, number[]> = {};
  for (const { key, levels } of PRESETS.contextSpec) {
    const spec = marginals[key] ?? PRESETS.baseMarginals[key];
    if (PRESETS.boolKeys.includes(key)) {
      const p = Number(spec);
      out[key] = [1 - p, p];
    } else {
      const w = levels.map((lv) => Number((spec as Record<string, number>)[lv] ?? 0));
      const total = w.reduce((a, b) => a + b, 0);
      out[key] = w.map((x) => x / total);
    }
  }
  return out;
}

/** One reader type: P(type | segment) and its four non-zero feature values. */
interface ContextCell {
  p: number;
  topic: number;
  interest: number;
  desktop: number;
  tablet: number;
}

function contextCells(lp: Record<string, number[]>): ContextCell[] {
  const cells: ContextCell[] = [];
  const dev = lp.devicetype; // mobile, desktop, tablet
  for (let d = 0; d < 3; d++) {
    for (let t = 0; t < 2; t++) {
      for (let i = 0; i < 2; i++) {
        const p = dev[d] * lp.topic_matches_trend[t] * lp.interest_matches_product[i];
        if (p > 0) cells.push({ p, topic: t, interest: i, desktop: d === 1 ? 1 : 0, tablet: d === 2 ? 1 : 0 });
      }
    }
  }
  return cells;
}

export interface PreviewInput {
  scenario: Scenario;
  ctrMode: CtrMode;
  /** One score map per creative, in creative (pipeline) order. */
  arms: ArmScores[];
  values: TuneValues;
}

export interface ScenarioPreview {
  segments: string[];
  /** Effective mix (sums to 1). */
  weights: number[];
  /** Expected click rate, creative × segment (pre-drift, before random variation). */
  ctr: number[][];
  /** Best creative index per segment. */
  oracle: number[];
  /** Mix-weighted expected click rate per creative. */
  overall: number[];
  /** Creative with the highest overall rate. */
  bestOverall: number;
  /** Best minus second-best overall rate (0 with one creative). */
  gap: number;
  /** Mix-weighted rate of showing every segment its best creative, minus bestOverall's rate. */
  contextGain: number;
  /** The judge's favourite (highest overall score). */
  judgeFavourite: number;
  /**
   * True when the judge's score gaps are smaller than the simulator's random
   * variation, so the real ranking is decided by chance (the preview can't show it).
   */
  judgeSignalWeak: boolean;
  /** Drift only: the best and worst creatives swap at `atFrac` of the run. */
  drift: { best: number; worst: number; atFrac: number } | null;
  alpha: number;
}

/** `build_true_model`'s deterministic structure → expected click rates (see module doc). */
export function previewMatrix({ scenario, ctrMode, arms, values }: PreviewInput): ScenarioPreview {
  const sc = SCENARIO_PRESETS[scenario];
  const k = arms.length;
  const segs = sc.segments;
  const S = segs.length;
  const weights = values.segmentMix.length === S ? normalise(values.segmentMix) : segs.map((s) => s.weight);
  const flip = 1 - 2 * values.judgeWrong;
  const g = values.gapScale;
  const scale = sc.targetCtr[ctrMode] / sc.targetCtr.demo;
  const target = sc.targetCtr[ctrMode];

  const adjusted = (dim: string) => {
    const raw = arms.map((a) => armScore(a, dim));
    const m = mean(raw);
    return raw.map((r) => flip * (r - m));
  };
  const adj: Record<string, number[]> = {};
  for (const dim of ["overall", "audience_fit", "trend_authenticity", "stopping_power", ...segs.map((s) => s.winnerKey)]) {
    adj[dim] ??= adjusted(dim);
  }

  // arm base
  let b: number[];
  if (sc.armEffect === "rank_ctrs") {
    const key = adj.overall.map((v) => -(sc.kappa * v));
    const order = [...Array(k).keys()].sort((x, y) => key[x] - key[y]); // stable
    const rl = rankLogits(spreadRankCtrs(sc.rankCtrs, g), k, scale);
    b = new Array<number>(k);
    order.forEach((arm, rank) => (b[arm] = rl[rank]));
    const bm = mean(b);
    b = b.map((v) => v - bm);
  } else {
    b = adj.overall.map((v) => sc.kappa * v);
  }

  // per-segment context distribution
  const lps = segs.map((s) => levelProbs(s.marginals));
  const cells = lps.map(contextCells);
  const pTopic = lps.map((lp) => lp.topic_matches_trend[1]);
  const pInterest = lps.map((lp) => lp.interest_matches_product[1]);
  const pDesktop = lps.map((lp) => lp.devicetype[1]);
  const pTablet = lps.map((lp) => lp.devicetype[2]);

  // segment affinity u[s][a]
  const u = segs.map((_, s) =>
    arms.map((_, a) => sc.lam * (pInterest[s] * adj.audience_fit[a] + pTopic[s] * adj.trend_authenticity[a]))
  );

  // θ_a on the four columns that survive without noise
  const th = arms.map((_, a) => ({
    topic: sc.eta * adj.trend_authenticity[a],
    interest: sc.eta * adj.audience_fit[a],
    device: -sc.eta * adj.stopping_power[a], // on desktop and tablet
  }));
  const thetaDot = (a: number, c: { topic: number; interest: number; desktop: number; tablet: number }) =>
    th[a].topic * c.topic + th[a].interest * c.interest + th[a].device * (c.desktop + c.tablet);

  if (sc.armEffect === "segment_winners") {
    const winners = greedyWinners(segs, adj, k);
    const lift = sc.liftPp * g; // apply_scenario_overrides: lift_pp × g
    const liftLogit = logit(target + lift * scale) - logit(target);
    winners.forEach((w, s) => {
      const mSeg = { topic: pTopic[s], interest: pInterest[s], desktop: pDesktop[s], tablet: pTablet[s] };
      const v = arms.map((_, a) => b[a] + u[s][a] + thetaDot(a, mSeg));
      const bestOther = Math.max(...v.filter((_, a) => a !== w));
      u[s][w] += bestOther + liftLogit - v[w];
    });
  }

  // logits without α, per segment × cell × arm
  const base = segs.map((_, s) => cells[s].map((c) => arms.map((_, a) => b[a] + u[s][a] + thetaDot(a, c))));
  const meanCtr = (alpha: number) => {
    let total = 0;
    for (let s = 0; s < S; s++) {
      let segSum = 0;
      cells[s].forEach((c, ci) => {
        let armSum = 0;
        for (let a = 0; a < k; a++) armSum += sigmoid(alpha + base[s][ci][a]);
        segSum += c.p * (armSum / k);
      });
      total += weights[s] * segSum;
    }
    return total;
  };
  let lo = -20;
  let hi = 10;
  for (let it = 0; it < 80; it++) {
    const mid = 0.5 * (lo + hi);
    if (meanCtr(mid) < target) lo = mid;
    else hi = mid;
  }
  const alpha = 0.5 * (lo + hi);

  const ctr = arms.map((_, a) =>
    segs.map((_, s) => cells[s].reduce((acc, c, ci) => acc + c.p * sigmoid(alpha + base[s][ci][a]), 0))
  );
  const oracle = segs.map((_, s) => argmax(ctr.map((row) => row[s])));
  const overall = ctr.map((row) => row.reduce((acc, v, s) => acc + weights[s] * v, 0));
  const bestOverall = argmax(overall);
  const sorted = [...overall].sort((x, y) => y - x);
  const oracleRate = segs.reduce((acc, _, s) => acc + weights[s] * ctr[oracle[s]][s], 0);

  const rawOverall = arms.map(armOverall);
  const spread = Math.max(...rawOverall) - Math.min(...rawOverall);
  const judgeSignalWeak = sc.kappa * Math.abs(flip) * spread < 2 * sc.noiseSd * values.noiseScale;

  return {
    segments: segs.map((s) => s.name),
    weights,
    ctr,
    oracle,
    overall,
    bestOverall,
    gap: k > 1 ? sorted[0] - sorted[1] : 0,
    contextGain: oracleRate - overall[bestOverall],
    judgeFavourite: argmax(rawOverall),
    judgeSignalWeak,
    drift:
      sc.drift.kind !== "none"
        ? { best: bestOverall, worst: argmin(overall), atFrac: values.driftAtFrac }
        : null,
    alpha,
  };
}

/** `_greedy_winners`: each segment takes the unassigned arm with the best `winner_key` score (ties → lower index). */
function greedyWinners(segs: SegmentPreset[], adj: Record<string, number[]>, k: number): number[] {
  let available: number[] = [];
  return segs.map((seg) => {
    if (!available.length) available = [...Array(k).keys()];
    const scores = adj[seg.winnerKey];
    let best = available[0];
    for (const a of available) if (scores[a] > scores[best]) best = a;
    available = available.filter((a) => a !== best);
    return best;
  });
}

/** First index of the maximum. */
function argmax(xs: number[]): number {
  let best = 0;
  for (let i = 1; i < xs.length; i++) if (xs[i] > xs[best]) best = i;
  return best;
}

function argmin(xs: number[]): number {
  let best = 0;
  for (let i = 1; i < xs.length; i++) if (xs[i] < xs[best]) best = i;
  return best;
}

// ── Reading ──────────────────────────────────────────────────────────────────

/** Percentage points, e.g. 0.018 → "1.8 pts" (two decimals below one point). */
export function formatPts(diff: number): string {
  const pts = Math.abs(diff) * 100;
  const text = pts >= 1 ? pts.toFixed(1) : pts.toFixed(2);
  return `${text} ${text === "1.0" ? "pt" : "pts"}`;
}

/**
 * One plain-language sentence about the preview: who prefers what and by how
 * much, then whether the judge's favourite agrees (and, for drift, the swap).
 */
export function previewReading(
  pv: ScenarioPreview,
  names: string[],
  segmentName: (s: string) => string
): string {
  const k = pv.overall.length;
  if (k < 2) return "";
  const name = (i: number) => names[i] ?? `Creative ${i + 1}`;
  const margin = (s: number) => {
    const col = pv.ctr.map((row) => row[s]);
    const best = pv.oracle[s];
    return col[best] - Math.max(...col.filter((_, a) => a !== best));
  };
  const distinct = new Set(pv.oracle);
  let first: string;
  if (distinct.size === 1) {
    const w = pv.oracle[0];
    first = `${name(w)} is the best creative for every segment, ${formatPts(pv.gap)} ahead overall`;
  } else {
    // the preference that matters most: margin weighted by the segment's share of readers
    const s = pv.segments
      .map((_, i) => i)
      .sort((x, y) => margin(y) * pv.weights[y] - margin(x) * pv.weights[x] || y - x)[0];
    first = `${segmentName(pv.segments[s])} will prefer ${name(pv.oracle[s])} by ${formatPts(margin(s))}, and ${
      distinct.size
    } different creatives win a segment`;
  }
  let judge: string;
  if (pv.judgeSignalWeak) {
    judge = "the judge's scores carry almost no signal here, so random variation decides which creative really leads";
  } else if (pv.judgeFavourite !== pv.bestOverall) {
    judge = `the judge favours ${name(pv.judgeFavourite)}, but ${name(
      pv.bestOverall
    )} gets the most clicks overall, so the bandit has to overrule the judge`;
  } else {
    judge = `the judge's favourite, ${name(pv.judgeFavourite)}, is also the best overall`;
  }
  let text = `${first}; ${judge}.`;
  if (pv.drift && pv.drift.best !== pv.drift.worst) {
    text += ` At ${Math.round(pv.drift.atFrac * 100)}% of the run, ${name(pv.drift.best)} and ${name(
      pv.drift.worst
    )} swap places.`;
  }
  return text;
}

// ── Plain-language labels (deploy panel, experiment page, insights) ──────────

/** Judge reliability in words, from judgeWrong (0 right … 0.5 no information … 1 backwards). */
export function judgeLabel(judgeWrong: number): string {
  if (judgeWrong <= EPS) return "Judge is right";
  if (judgeWrong <= 0.25 + EPS) return "Judge is mostly right";
  if (judgeWrong < 0.5 - EPS) return "Judge leans right";
  if (judgeWrong <= 0.5 + EPS) return "No information";
  if (judgeWrong < 0.75 - EPS) return "Judge leans backwards";
  if (judgeWrong < 1 - EPS) return "Judge is mostly backwards";
  return "Judge is backwards";
}

/** "1.5×" / "0.25×". */
export function formatScale(v: number): string {
  return `${Number(v.toFixed(2))}×`;
}

/** Segment keys arrive snake_case ("mobile_scrollers"); show them as words. */
export function segmentWords(s: string): string {
  const t = s.replace(/[_-]+/g, " ").trim();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

/**
 * The dominant segment when the mix is skewed (one segment has at least half the
 * readers, or 4× the smallest), else null.
 */
export function skewedSegment(scenario: string, mix: number[] | undefined): { name: string; share: number } | null {
  const segs = SCENARIO_PRESETS[scenario as Scenario]?.segments;
  if (!segs || !mix || mix.length !== segs.length) return null;
  const w = normalise(mix);
  const top = argmax(w);
  const skewed = w[top] >= 0.5 - EPS || w[top] >= 4 * Math.min(...w) - EPS;
  return skewed ? { name: segs[top].name, share: w[top] } : null;
}

export interface OverrideLine {
  label: string;
  value: string;
  /** The preset's value, for comparison. */
  preset: string;
}

/** One plain-language line per override that is set (experiment page disclosure). */
export function describeOverrides(scenario: string, ov: ScenarioOverrides | null | undefined): OverrideLine[] {
  if (!ov || !(scenario in SCENARIO_PRESETS)) return [];
  const sc = scenario as Scenario;
  const base = presetValues(sc);
  const segs = SCENARIO_PRESETS[sc].segments;
  const lines: OverrideLine[] = [];
  const mixText = (mix: number[]) => {
    const ps = displayPercents(normalise(mix));
    if (normalise(mix).every((w) => Math.abs(w - 1 / mix.length) < EPS)) return `${ps[0]}% each`;
    return ps.map((p, i) => `${segmentWords(segs[i].name)} ${p}%`).join(", ");
  };
  if (ov.segmentMix?.length === segs.length) {
    lines.push({ label: "Audience mix", value: mixText(ov.segmentMix), preset: mixText(base.segmentMix) });
  }
  if (typeof ov.gapScale === "number") {
    const dir = ov.gapScale > 1 ? ", more obvious" : ov.gapScale < 1 ? ", more subtle" : "";
    lines.push({ label: "Gap between creatives", value: `${formatScale(ov.gapScale)}${dir}`, preset: "1×" });
  }
  if (typeof ov.judgeWrong === "number") {
    lines.push({ label: "Judge reliability", value: judgeLabel(ov.judgeWrong), preset: judgeLabel(base.judgeWrong) });
  }
  if (typeof ov.noiseScale === "number") {
    lines.push({
      label: "Random variation",
      value: ov.noiseScale === 0 ? "None" : formatScale(ov.noiseScale),
      preset: "1×",
    });
  }
  if (typeof ov.driftAtFrac === "number") {
    lines.push({
      label: "Drift point",
      value: `${Math.round(ov.driftAtFrac * 100)}% of the run`,
      preset: `${Math.round(base.driftAtFrac * 100)}% of the run`,
    });
  }
  return lines;
}
