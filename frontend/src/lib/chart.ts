/**
 * Pure chart primitives for the hand-rolled SVG charts (no chart dependency):
 * linear/log scales, nice ticks, path builders, downsampling and number
 * formatting. Everything here is deterministic and unit-tested.
 */

export type Scale = (v: number) => number;

/** Map `[d0, d1]` linearly onto `[r0, r1]`. A zero-width domain maps to the range midpoint. */
export function scaleLinear(domain: [number, number], range: [number, number]): Scale {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0;
  if (span === 0) return () => (r0 + r1) / 2;
  return (v) => r0 + ((v - d0) / span) * (r1 - r0);
}

/** Map `[d0, d1]` (both > 0) onto `[r0, r1]` on a base-10 log scale; values ≤ 0 clamp to d0. */
export function scaleLog(domain: [number, number], range: [number, number]): Scale {
  const d0 = Math.max(domain[0], Number.MIN_VALUE);
  const d1 = Math.max(domain[1], d0);
  const lin = scaleLinear([Math.log10(d0), Math.log10(d1)], range);
  return (v) => lin(Math.log10(v > 0 ? v : d0));
}

/** [min, max] of the finite values, or null when there are none. */
export function extent(values: Iterable<number>): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (!Number.isFinite(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  return lo === Infinity ? null : [lo, hi];
}

/** A "nice" step (1, 2, 2.5 or 5 × 10^k) for roughly `count` intervals over `span`. */
export function niceStep(span: number, count = 5): number {
  if (!(span > 0) || !Number.isFinite(span)) return 1;
  const raw = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return nice * mag;
}

/** Round to the step's precision so 0.1 + 0.2 style float noise never reaches a label. */
function snap(v: number, step: number): number {
  const decimals = Math.max(0, -Math.floor(Math.log10(step)) + 2);
  return Number(v.toFixed(decimals));
}

/** Expand `[min, max]` outward to whole multiples of a nice step. */
export function niceDomain(min: number, max: number, count = 5): [number, number] {
  if (min === max) {
    const pad = min === 0 ? 1 : Math.abs(min) * 0.1;
    return niceDomain(min - pad, max + pad, count);
  }
  const step = niceStep(max - min, count);
  return [snap(Math.floor(min / step) * step, step), snap(Math.ceil(max / step) * step, step)];
}

/** Evenly spaced nice ticks inside `[min, max]` (inclusive). */
export function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) return [min];
  const step = niceStep(max - min, count);
  const start = Math.ceil(min / step - 1e-9) * step;
  const ticks: number[] = [];
  for (let v = start; v <= max + step * 1e-9; v += step) ticks.push(snap(v, step));
  return ticks;
}

export interface LogTick {
  value: number;
  /** Decade (10^k) ticks are labelled; 2× and 5× minors only when the span is short. */
  major: boolean;
}

/**
 * Ticks for a base-10 log axis over `[min, max]` (min > 0): every decade, plus
 * 2× and 5× minors when the domain spans fewer than three decades.
 */
export function logTicks(min: number, max: number): LogTick[] {
  if (!(min > 0) || !(max >= min)) return [];
  const k0 = Math.floor(Math.log10(min) + 1e-9);
  const k1 = Math.ceil(Math.log10(max) - 1e-9);
  const withMinors = k1 - k0 < 3;
  const ticks: LogTick[] = [];
  for (let k = k0; k <= k1; k++) {
    const decade = 10 ** k;
    for (const m of withMinors ? [1, 2, 5] : [1]) {
      const v = Number((m * decade).toPrecision(12));
      if (v >= min * (1 - 1e-9) && v <= max * (1 + 1e-9)) ticks.push({ value: v, major: m === 1 });
    }
  }
  return ticks;
}

export interface Point {
  x: number;
  y: number;
}

const r2 = (v: number) => Math.round(v * 100) / 100;

/** SVG polyline path ("M…L…"); non-finite points break the line into segments. */
export function linePath(points: Point[]): string {
  let d = "";
  let pen = false;
  for (const p of points) {
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : d ? " M" : "M"}${r2(p.x)} ${r2(p.y)}`;
    pen = true;
  }
  return d;
}

/**
 * Closed area path for a confidence band: along `hi` left→right, then back
 * along `lo` right→left. Points with a non-finite coordinate are skipped.
 */
export function bandPath(points: { x: number; lo: number; hi: number }[]): string {
  const ok = points.filter((p) => [p.x, p.lo, p.hi].every(Number.isFinite));
  if (ok.length < 2) return "";
  const top = ok.map((p, i) => `${i ? "L" : "M"}${r2(p.x)} ${r2(p.hi)}`).join("");
  const bottom = [...ok].reverse().map((p) => `L${r2(p.x)} ${r2(p.lo)}`).join("");
  return `${top}${bottom}Z`;
}

/** At most `max` indices into an array of length `n`, evenly spaced, always keeping first and last. */
export function downsampleIndices(n: number, max: number): number[] {
  if (n <= 0) return [];
  if (n <= max || max < 2) return Array.from({ length: max < 2 ? Math.min(n, 1) : n }, (_, i) => i);
  const out: number[] = [];
  for (let i = 0; i < max; i++) {
    const idx = Math.round((i * (n - 1)) / (max - 1));
    if (out[out.length - 1] !== idx) out.push(idx);
  }
  return out;
}

/** Keep at most `max` items (see downsampleIndices). */
export function downsample<T>(items: T[], max: number): T[] {
  return downsampleIndices(items.length, max).map((i) => items[i]);
}

/** Index of the item whose `x` is closest to `x` (items sorted by x); -1 when empty. */
export function nearestIndex(xs: number[], x: number): number {
  if (xs.length === 0) return -1;
  let lo = 0;
  let hi = xs.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] <= x) lo = mid;
    else hi = mid;
  }
  return Math.abs(xs[hi] - x) < Math.abs(xs[lo] - x) ? hi : lo;
}

/** Compact number: 20000 → "20k", 1500 → "1.5k", 2.5e6 → "2.5M", 0.0423 → "0.042". */
export function formatCompact(v: number): string {
  if (!Number.isFinite(v)) return "–";
  const a = Math.abs(v);
  const trim = (s: string) => s.replace(/\.0+$|(\.\d*[1-9])0+$/, "$1");
  if (a >= 1e6) return `${trim((v / 1e6).toFixed(1))}M`;
  if (a >= 1e3) return `${trim((v / 1e3).toFixed(1))}k`;
  if (a >= 100 || a === 0) return `${Math.round(v)}`;
  if (a >= 1) return trim(v.toFixed(1));
  return trim(v.toPrecision(2));
}

/** 0–1 fraction → "42%" (or "4.2%" with digits=1). */
export function formatPercent(v: number, digits = 0): string {
  if (!Number.isFinite(v)) return "–";
  return `${(v * 100).toFixed(digits)}%`;
}

/** Thousands-separated integer, e.g. 20000 → "20,000". */
export function formatInt(v: number): string {
  if (!Number.isFinite(v)) return "–";
  return Math.round(v).toLocaleString("en-US");
}

/**
 * Nudge label y positions apart so no two are closer than `gap`, keeping them
 * inside `[min, max]`. Returns positions in the input order.
 */
export function spreadLabels(ys: number[], gap: number, min: number, max: number): number[] {
  const order = ys.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y);
  const pos = order.map((o) => o.y);
  for (let k = 1; k < pos.length; k++) pos[k] = Math.max(pos[k], pos[k - 1] + gap);
  const overflow = pos.length ? pos[pos.length - 1] - max : 0;
  if (overflow > 0) for (let k = 0; k < pos.length; k++) pos[k] -= overflow;
  for (let k = 0; k < pos.length; k++) pos[k] = Math.max(pos[k], min + k * gap);
  const out = new Array<number>(ys.length);
  order.forEach((o, k) => (out[o.i] = pos[k]));
  return out;
}
