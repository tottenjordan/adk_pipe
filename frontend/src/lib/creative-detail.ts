/**
 * Pure helpers for the creative detail drawer (experiment Overview): observed
 * numbers for one creative, read against the whole set and per audience segment,
 * plus the one plain-language reading sentence at the top of the drawer.
 *
 * Everything is optional-safe: the per-segment fields (`segments`, `missedClicks`,
 * `engagedSecondsPer1k`, contracts §8) arrive from newer APIs only, and each
 * derived value is null when the data behind it is missing, so the drawer hides
 * that section instead of showing a made-up number.
 */
import { formatInt, formatPercent, niceStep } from "./chart";
import type { CreativeSeries, CreativeSeriesItem } from "./experiments";

/** Fewer clicks than this overall and the drawer says the data is too thin to read. */
export const MIN_CLICKS = 30;
/** A segment needs this much evidence before the reading sentence makes a claim about it. */
export const MIN_SEGMENT_IMPRESSIONS = 500;
export const MIN_SEGMENT_CLICKS = 20;
/** Relative differences smaller than this read as "about the same". */
export const FLAT_DIFF = 0.1;

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export interface DetailSegment {
  segment: string;
  impressions: number;
  clicks: number;
  /** Observed clicks / impressions (recomputed when the API sent null); null with no impressions. */
  ctr: number | null;
  trueCtr: number | null;
  isBest: boolean;
  /** The creative that is best for this segment (from any creative's `isBest`), or null. */
  bestCreativeId: string | null;
}

export interface CreativeDetail {
  creativeId: string;
  impressions: number | null;
  clicks: number | null;
  clickRate: number | null;
  trueCtr: number | null;
  /** This creative's click rate ÷ the pooled click rate of every creative − 1. */
  lift: number | null;
  /** Share of all impressions / all clicks over the run. */
  trafficShare: number | null;
  clickShare: number | null;
  /** Mean per episode, vs the best creative for each reader. */
  missedClicks: number | null;
  /** Mean clicks per episode (context for missed clicks). */
  clicksPerEpisode: number | null;
  engagedSecondsPer1k: number | null;
  /** Observed click rate per window, at the window's mid round; null windows are dropped. */
  ctrPoints: { x: number; y: number }[];
  segments: DetailSegment[];
  /** Number of segments this creative is best for (from `segments`, else `segmentsWon`). */
  bestCount: number;
  /** Upper bound of the segment bar scale, shared by every creative. */
  segmentMax: number;
  /** Too little data to read a click rate with confidence. */
  thin: boolean;
  reading: string;
}

/** Clicks / impressions over every creative in the series; null with no impressions. */
export function pooledCtr(series: Pick<CreativeSeries, "creatives"> | null | undefined): number | null {
  let imps = 0;
  let clicks = 0;
  for (const c of series?.creatives ?? []) {
    if (finite(c.impressions) && finite(c.clicks) && c.impressions > 0) {
      imps += c.impressions;
      clicks += c.clicks;
    }
  }
  return imps > 0 ? clicks / imps : null;
}

const rate = (c: Pick<CreativeSeriesItem, "impressions" | "clicks"> | null | undefined): number | null =>
  c && finite(c.impressions) && finite(c.clicks) && c.impressions > 0 ? c.clicks / c.impressions : null;

/**
 * Lift vs the set: this creative's click rate over the pooled rate of all
 * creatives, minus one (+0.05 = 5% above). Null without data, or with a single
 * creative (it would be compared with itself).
 */
export function liftVsSet(
  item: Pick<CreativeSeriesItem, "impressions" | "clicks"> | null | undefined,
  series: Pick<CreativeSeries, "creatives"> | null | undefined
): number | null {
  const mine = rate(item);
  const pooled = pooledCtr(series);
  const others = (series?.creatives ?? []).filter((c) => rate(c) !== null).length;
  if (mine === null || pooled === null || pooled <= 0 || others < 2) return null;
  return mine / pooled - 1;
}

/** This creative's share of all impressions and of all clicks over the run. */
export function clickTrafficShare(
  item: Pick<CreativeSeriesItem, "impressions" | "clicks"> | null | undefined,
  series: Pick<CreativeSeries, "creatives"> | null | undefined
): { traffic: number | null; clicks: number | null } {
  let imps = 0;
  let clicks = 0;
  for (const c of series?.creatives ?? []) {
    if (finite(c.impressions)) imps += c.impressions;
    if (finite(c.clicks)) clicks += c.clicks;
  }
  return {
    traffic: item && finite(item.impressions) && imps > 0 ? item.impressions / imps : null,
    clicks: item && finite(item.clicks) && clicks > 0 ? item.clicks / clicks : null,
  };
}

const segmentCtr = (s: { impressions: number; clicks: number; ctr: number | null }): number | null =>
  finite(s.ctr) ? s.ctr : finite(s.impressions) && finite(s.clicks) && s.impressions > 0 ? s.clicks / s.impressions : null;

/** Segment → id of the creative marked best for it (first one wins on a malformed tie). */
export function bestBySegment(series: Pick<CreativeSeries, "creatives"> | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const c of series?.creatives ?? []) {
    for (const s of c.segments ?? []) if (s.isBest && !(s.segment in out)) out[s.segment] = c.creativeId;
  }
  return out;
}

/**
 * Upper bound of the segment bar scale: the highest observed or true segment
 * click rate across every creative, plus 5% headroom, rounded up to a nice step.
 * Shared by all creatives so switching between them keeps bars comparable.
 * 0 when there are no segment rates (the section is hidden).
 */
export function segmentScaleMax(series: Pick<CreativeSeries, "creatives"> | null | undefined): number {
  let max = 0;
  for (const c of series?.creatives ?? []) {
    for (const s of c.segments ?? []) {
      const v = segmentCtr(s);
      if (finite(v) && v > max) max = v;
      if (finite(s.trueCtr) && s.trueCtr > max) max = s.trueCtr;
    }
  }
  if (max <= 0) return 0;
  const step = niceStep(max, 4);
  return Math.min(1, Math.ceil((max * 1.05) / step - 1e-9) * step);
}

/** "late_night_casual" → "late night casual" (inside a sentence). */
export function segmentWords(s: string): string {
  return s.replace(/[_-]+/g, " ").trim().toLowerCase();
}

/** Signed percent with a true minus: +4.8%, −12%, 0%. One decimal under 10%. */
export function formatSignedPercent(v: number): string {
  if (!Number.isFinite(v)) return "–";
  const a = Math.abs(v);
  const digits = a < 0.1 ? 1 : 0;
  const body = (a * 100).toFixed(digits);
  if (Number(body) === 0) return "0%";
  return `${v < 0 ? "−" : "+"}${body}%`;
}

function pickClause(best: number, total: number): string {
  if (total <= 0) return "";
  if (best <= 0) return `it was not the best pick for any of the ${total} segments`;
  if (best >= total) return total === 1 ? "it was the right pick for the one segment" : `it was the right pick for all ${total} segments`;
  return `it was the right pick for ${best} of ${total} segments`;
}

/**
 * One plain-language sentence about the creative, from observed data only:
 * where it earns most (vs its own rate with other readers) and how many
 * segments it was the right pick for; honest when the data is thin or missing.
 */
export function readingSentence(
  item: CreativeSeriesItem | null | undefined,
  series: Pick<CreativeSeries, "creatives"> | null | undefined
): string {
  if (!item || !finite(item.impressions) || item.impressions <= 0) {
    return "No readers have seen this creative yet.";
  }
  const clicks = finite(item.clicks) ? item.clicks : 0;
  if (clicks < MIN_CLICKS) {
    return `Only ${formatInt(clicks)} ${clicks === 1 ? "click" : "clicks"} from ${formatInt(
      item.impressions
    )} impressions so far: too few to say how this creative performs.`;
  }
  const overall = clicks / item.impressions;
  const segs = (item.segments ?? []).filter((s) => finite(s.impressions) && finite(s.clicks));

  if (segs.length >= 2) {
    const total = segs.length;
    const best = segs.filter((s) => s.isBest).length;
    const solid = segs
      .map((s) => ({ s, ctr: segmentCtr(s) }))
      .filter(
        (r): r is { s: (typeof segs)[number]; ctr: number } =>
          r.ctr !== null && r.s.impressions >= MIN_SEGMENT_IMPRESSIONS && r.s.clicks >= MIN_SEGMENT_CLICKS
      );
    const clause = pickClause(best, total);
    if (solid.length >= 2) {
      const top = solid.reduce((a, b) => (b.ctr > a.ctr ? b : a));
      const restImps = item.impressions - top.s.impressions;
      const restClicks = clicks - top.s.clicks;
      const rest = restImps >= MIN_SEGMENT_IMPRESSIONS ? restClicks / restImps : null;
      if (rest !== null && rest > 0) {
        const diff = top.ctr / rest - 1;
        if (diff >= FLAT_DIFF) {
          return `Earns ${formatPercent(top.ctr, 1)} with ${segmentWords(top.s.segment)}, ${formatPercent(
            diff
          )} above its rate with other readers; ${clause}.`;
        }
        const ctrs = solid.map((r) => r.ctr);
        return `Earns about the same with every segment (${formatPercent(Math.min(...ctrs), 1)} to ${formatPercent(
          Math.max(...ctrs),
          1
        )}); ${clause}.`;
      }
    }
    return `Earns ${formatPercent(overall, 1)} overall, with too few clicks per segment to compare them; ${clause}.`;
  }

  // No per-segment data: compare with the set, and count wins from segmentsWon.
  const lift = liftVsSet(item, series);
  const vsSet =
    lift === null
      ? ""
      : Math.abs(lift) < 0.02
        ? ", in line with the pooled rate of all creatives"
        : `, ${formatPercent(Math.abs(lift))} ${lift > 0 ? "above" : "below"} the pooled rate of all creatives`;
  const won = item.segmentsWon?.length ?? 0;
  const wins = won ? `; it is the best choice for ${won} ${won === 1 ? "segment" : "segments"}` : "";
  return `Earns ${formatPercent(overall, 1)} overall${vsSet}${wins}.`;
}

export interface SegmentGrid {
  /** Segment keys, in the API's order (sorted by name). */
  segments: string[];
  rows: {
    creativeId: string;
    cells: { segment: string; ctr: number | null; impressions: number; isBest: boolean }[];
  }[];
  /** Observed click-rate range across every cell (the shading domain); null with no rates. */
  range: [number, number] | null;
}

/**
 * The creative × segment click-rate grid on the Overview, rows in `order`
 * (scoreboard rank; creatives missing from it follow). Null unless at least one
 * creative has two or more segments, so the grid is skipped on older APIs.
 */
export function segmentGrid(
  series: Pick<CreativeSeries, "creatives"> | null | undefined,
  order: string[] = []
): SegmentGrid | null {
  const creatives = (series?.creatives ?? []).filter((c) => (c.segments?.length ?? 0) > 0);
  const segments = [...new Set(creatives.flatMap((c) => (c.segments ?? []).map((s) => s.segment)))].sort();
  if (segments.length < 2 || creatives.length < 2) return null;
  const pos = (id: string) => {
    const i = order.indexOf(id);
    return i >= 0 ? i : order.length;
  };
  const sorted = [...creatives].sort((a, b) => pos(a.creativeId) - pos(b.creativeId));
  let lo = Infinity;
  let hi = -Infinity;
  const rows = sorted.map((c) => ({
    creativeId: c.creativeId,
    cells: segments.map((seg) => {
      const s = c.segments?.find((x) => x.segment === seg);
      const ctr = s ? segmentCtr(s) : null;
      if (ctr !== null) {
        lo = Math.min(lo, ctr);
        hi = Math.max(hi, ctr);
      }
      return { segment: seg, ctr, impressions: s && finite(s.impressions) ? s.impressions : 0, isBest: Boolean(s?.isBest) };
    }),
  }));
  return { segments, rows, range: Number.isFinite(lo) ? [lo, hi] : null };
}

/** 0–1 shading strength for a grid cell (0 at the lowest rate, 1 at the highest; 0.5 when all equal). */
export function cellShade(ctr: number | null, range: [number, number] | null): number {
  if (ctr === null || !range) return 0;
  const [lo, hi] = range;
  if (hi - lo <= 1e-12) return 0.5;
  return Math.max(0, Math.min(1, (ctr - lo) / (hi - lo)));
}

/** Everything the drawer shows for one creative. */
export function buildCreativeDetail(
  item: CreativeSeriesItem | null | undefined,
  series: Pick<CreativeSeries, "creatives" | "windows" | "episodes"> | null | undefined
): CreativeDetail | null {
  if (!item) return null;
  const share = clickTrafficShare(item, series);
  const bestFor = bestBySegment(series);
  const segments: DetailSegment[] = (item.segments ?? [])
    .filter((s) => typeof s?.segment === "string")
    .map((s) => ({
      segment: s.segment,
      impressions: finite(s.impressions) ? s.impressions : 0,
      clicks: finite(s.clicks) ? s.clicks : 0,
      ctr: segmentCtr(s),
      trueCtr: finite(s.trueCtr) ? s.trueCtr : null,
      isBest: Boolean(s.isBest),
      bestCreativeId: bestFor[s.segment] ?? null,
    }));
  const windows = series?.windows ?? [];
  const ctrPoints = (item.ctr ?? [])
    .map((v, i) => {
      const w = windows[i];
      return { x: w ? (w.start + w.end) / 2 : i, y: v };
    })
    .filter((p): p is { x: number; y: number } => finite(p.y));
  const episodes = series?.episodes ?? 0;
  const clicks = finite(item.clicks) ? item.clicks : null;
  const impressions = finite(item.impressions) ? item.impressions : null;
  return {
    creativeId: item.creativeId,
    impressions,
    clicks,
    clickRate: rate(item),
    trueCtr: finite(item.trueCtr) ? item.trueCtr : null,
    lift: liftVsSet(item, series),
    trafficShare: share.traffic,
    clickShare: share.clicks,
    missedClicks: finite(item.missedClicks) ? item.missedClicks : null,
    clicksPerEpisode: clicks !== null && episodes > 0 ? clicks / episodes : null,
    engagedSecondsPer1k: finite(item.engagedSecondsPer1k) ? item.engagedSecondsPer1k : null,
    ctrPoints,
    segments,
    bestCount: segments.length ? segments.filter((s) => s.isBest).length : (item.segmentsWon?.length ?? 0),
    segmentMax: segmentScaleMax(series),
    thin: (clicks ?? 0) < MIN_CLICKS,
    reading: readingSentence(item, series),
  };
}
