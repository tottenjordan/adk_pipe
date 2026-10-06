/**
 * Pure helpers for the creative scoreboard (experiment Overview): one lane per
 * creative, ranked by its share of traffic in the last window, with a share-over-
 * time strip on a y scale shared by every lane.
 */
import {
  armColor,
  armName,
  shortId,
  type Arm,
  type CreativeSeries,
  type ExperimentMetrics,
} from "./experiments";
import type { MetricRegime, SeriesRegime } from "./shifts";

export interface Lane {
  creativeId: string;
  arm: Arm | null;
  rank: number;
  name: string;
  headline: string;
  color: string;
  /** Share of the endpoint's traffic in the last window (null before any traffic). */
  finalShare: number | null;
  /** Share per window, oldest first (empty without the creative series). */
  share: number[];
  impressions: number | null;
  clicks: number | null;
  /** Observed clicks / impressions; null with no impressions or no series. */
  clickRate: number | null;
  trueCtr: number | null;
  segmentsWon: string[];
}

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** Even-split share for `k` creatives (the strip's reference line); 0 with none. */
export function evenSplit(k: number): number {
  return k > 0 ? 1 / k : 0;
}

/** Highest share first; unknown shares last; ties keep arm order. */
export function rankLanes<T extends { finalShare: number | null; arm: Arm | null }>(lanes: T[]): T[] {
  const idx = (l: T) => l.arm?.index ?? Number.MAX_SAFE_INTEGER;
  return [...lanes].sort((a, b) => {
    const sa = a.finalShare ?? -1;
    const sb = b.finalShare ?? -1;
    return sb !== sa ? sb - sa : idx(a) - idx(b);
  });
}

/**
 * Upper bound of the y scale every strip shares: the largest share any lane
 * reaches (and the even split), with 10% headroom, rounded up to the next 10
 * points and capped at 100%. Always starts from 0, so lanes compare honestly.
 */
export function sharedShareMax(lanes: Pick<Lane, "share" | "finalShare">[], k = lanes.length): number {
  let max = evenSplit(k);
  for (const l of lanes) {
    for (const v of l.share) if (finite(v) && v > max) max = v;
    if (finite(l.finalShare) && l.finalShare > max) max = l.finalShare;
  }
  if (max <= 0) return 1;
  return Math.min(1, Math.ceil((max * 1.1 - 1e-9) * 10) / 10);
}

/**
 * Line and area paths for a share strip in a `width` × `height` box with y in
 * [0, yMax]. Values are clamped to the box; non-finite values are skipped.
 */
export function stripPaths(
  share: number[],
  yMax: number,
  width: number,
  height: number
): { line: string; area: string; points: { x: number; y: number; v: number }[] } {
  const n = share.length;
  const pts = share
    .map((v, i) => ({ v, i }))
    .filter(({ v }) => finite(v))
    .map(({ v, i }) => {
      const x = n <= 1 ? width : (i / (n - 1)) * width;
      const y = height - (Math.max(0, Math.min(v, yMax)) / (yMax || 1)) * height;
      return { x: round(x), y: round(y), v };
    });
  if (!pts.length) return { line: "", area: "", points: [] };
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join("");
  const area = `${line}L${pts[pts.length - 1].x},${height}L${pts[0].x},${height}Z`;
  return { line, area, points: pts };
}

const round = (v: number) => Math.round(v * 100) / 100;

/** y position of a share value in the strip box. */
export function shareY(v: number, yMax: number, height: number): number {
  return round(height - (Math.max(0, Math.min(v, yMax)) / (yMax || 1)) * height);
}

/**
 * One lane per creative: the arms (so every creative shows even before traffic),
 * filled from the creative series when present, else from the metrics payload.
 */
export function buildLanes(
  arms: Arm[],
  metrics: ExperimentMetrics | null | undefined,
  series: CreativeSeries | null | undefined
): Lane[] {
  const byId = new Map(arms.map((a) => [a.creativeId, a]));
  const seriesById = new Map((series?.creatives ?? []).map((c) => [c.creativeId, c]));
  const ids = [...new Set([...arms.map((a) => a.creativeId), ...seriesById.keys()])];
  const won = (id: string) =>
    Object.entries(metrics?.perSegment ?? {})
      .filter(([, s]) => s.optimalArm === id)
      .map(([seg]) => seg)
      .sort();

  const lanes = ids.map((id) => {
    const arm = byId.get(id) ?? null;
    const c = seriesById.get(id);
    const row = metrics?.arms?.find((r) => r.creativeId === id);
    const metricShare = metrics?.armShare?.[id];
    const lastMetricShare = metricShare?.length ? metricShare[metricShare.length - 1] : null;
    const finalShare = c
      ? finite(c.finalShare)
        ? c.finalShare
        : finite(c.share?.at(-1))
          ? (c.share.at(-1) as number)
          : null
      : finite(lastMetricShare) && (metrics?.episodes ?? 0) > 0
        ? lastMetricShare
        : null;
    const trueCtr = finite(c?.trueCtr) ? (c?.trueCtr as number) : finite(row?.trueCtr) ? (row?.trueCtr as number) : null;
    return {
      creativeId: id,
      arm,
      rank: 0,
      name: arm ? armName(arm) : shortId(id),
      headline: arm?.label && arm.label !== armName(arm) ? arm.label : "",
      color: armColor(arms, id),
      finalShare,
      share: (c?.share ?? []).filter(finite),
      impressions: c && finite(c.impressions) ? c.impressions : null,
      clicks: c && finite(c.clicks) ? c.clicks : null,
      clickRate: c && c.impressions > 0 && finite(c.clicks) ? c.clicks / c.impressions : null,
      trueCtr: (metrics?.episodes ?? 0) > 0 || c ? trueCtr : null,
      segmentsWon: c?.segmentsWon?.length ? [...c.segmentsWon].sort() : won(id),
    };
  });
  return rankLanes(lanes).map((l, i) => ({ ...l, rank: i + 1 }));
}

// ── Regimes (scripted shifts, contracts §10) ─────────────────────────────────

/** One creative's numbers in one period of the run. */
export interface LanePeriod {
  label: string;
  start: number;
  end: number;
  clickRate: number | null;
  trueCtr: number | null;
  segmentsWon: string[];
}

/** A lane's latest period (what "Click rate" / "Segments won" show) and the first one ("before"). */
export interface LaneRegime {
  latest: LanePeriod;
  before: LanePeriod;
}

/**
 * Per-period numbers for one creative from the run's regimes: the observed click
 * rate (series regimes), the true rate (series, else metrics regimes) and the
 * segments it is best for. [] without at least two regimes.
 */
export function lanePeriods(
  creativeId: string,
  series: SeriesRegime[],
  metric: MetricRegime[],
  labels: string[]
): LanePeriod[] {
  const n = Math.max(series.length, metric.length);
  if (n < 2) return [];
  return Array.from({ length: n }, (_, i) => {
    const sr = series[i];
    const mr = metric.find((m) => m.start === (sr?.start ?? metric[i]?.start)) ?? metric[i];
    const c = sr?.creatives.find((x) => x.creativeId === creativeId);
    const won =
      sr && Object.keys(sr.optimal).length
        ? Object.entries(sr.optimal)
            .filter(([, id]) => id === creativeId)
            .map(([s]) => s)
        : Object.entries(mr?.perSegment ?? {})
            .filter(([, v]) => v.optimalArm === creativeId)
            .map(([s]) => s);
    const t = c?.trueCtr ?? mr?.trueCtr?.[creativeId];
    return {
      label: labels[i] ?? `Period ${i + 1}`,
      start: sr?.start ?? mr?.start ?? 0,
      end: sr?.end ?? mr?.end ?? 0,
      clickRate: c?.ctr ?? null,
      trueCtr: finite(t) ? t : null,
      segmentsWon: won.sort(),
    };
  });
}

/** Latest + before periods for each lane, by creativeId ({} without regimes). */
export function laneRegimes(
  lanes: Pick<Lane, "creativeId">[],
  series: SeriesRegime[],
  metric: MetricRegime[],
  labels: string[]
): Record<string, LaneRegime> {
  const out: Record<string, LaneRegime> = {};
  for (const l of lanes) {
    const ps = lanePeriods(l.creativeId, series, metric, labels);
    if (ps.length >= 2) out[l.creativeId] = { latest: ps[ps.length - 1], before: ps[0] };
  }
  return out;
}

/** Which period a round falls in (index into the regimes), or -1. */
export function periodAt(regimes: { start: number; end: number }[], round: number): number {
  return regimes.findIndex((g) => round >= g.start && round < g.end);
}
