import { describe, expect, it } from "vitest";
import {
  bandPath,
  downsample,
  downsampleIndices,
  extent,
  formatCompact,
  formatInt,
  formatPercent,
  linePath,
  logTicks,
  nearestIndex,
  niceDomain,
  niceStep,
  niceTicks,
  scaleLinear,
  scaleLog,
  spreadLabels,
} from "@/lib/chart";

describe("scales", () => {
  it("maps linearly, including inverted ranges (SVG y)", () => {
    const x = scaleLinear([0, 10], [0, 100]);
    expect(x(5)).toBe(50);
    const y = scaleLinear([0, 1], [200, 0]);
    expect(y(0)).toBe(200);
    expect(y(0.25)).toBe(150);
  });
  it("maps a zero-width domain to the range midpoint", () => {
    expect(scaleLinear([3, 3], [0, 100])(3)).toBe(50);
  });
  it("maps decades to equal widths on a log scale and clamps non-positive values", () => {
    const x = scaleLog([10, 10000], [0, 300]);
    expect(x(10)).toBeCloseTo(0);
    expect(x(100)).toBeCloseTo(100);
    expect(x(1000)).toBeCloseTo(200);
    expect(x(10000)).toBeCloseTo(300);
    expect(x(0)).toBeCloseTo(0);
    expect(x(-5)).toBeCloseTo(0);
  });
  it("finds the finite extent", () => {
    expect(extent([3, NaN, -1, Infinity, 7])).toEqual([-1, 7]);
    expect(extent([])).toBeNull();
  });
});

describe("nice ticks", () => {
  it("picks 1/2/2.5/5 steps", () => {
    expect(niceStep(10, 5)).toBe(2);
    expect(niceStep(1, 4)).toBe(0.25);
    expect(niceStep(0.07, 5)).toBeCloseTo(0.02);
    expect(niceStep(0, 5)).toBe(1);
  });
  it("produces clean ticks without float noise", () => {
    expect(niceTicks(0, 10, 5)).toEqual([0, 2, 4, 6, 8, 10]);
    expect(niceTicks(0, 0.3, 3)).toEqual([0, 0.1, 0.2, 0.3]);
    expect(niceTicks(0.013, 0.061, 4)).toEqual([0.02, 0.04, 0.06]);
    expect(niceTicks(5, 5)).toEqual([5]);
  });
  it("expands a domain to nice bounds", () => {
    expect(niceDomain(0.013, 0.061, 4)).toEqual([0, 0.08]);
    expect(niceDomain(3, 97, 5)).toEqual([0, 100]);
    const [lo, hi] = niceDomain(0, 0);
    expect(lo).toBeLessThan(0);
    expect(hi).toBeGreaterThan(0);
  });
  it("labels every decade on a log axis", () => {
    const t = logTicks(100, 20000);
    expect(t.filter((x) => x.major).map((x) => x.value)).toEqual([100, 1000, 10000]);
  });
  it("adds 2× and 5× minors only over short spans", () => {
    expect(logTicks(100, 1000).map((t) => t.value)).toEqual([100, 200, 500, 1000]);
    expect(logTicks(1, 100000).every((t) => t.major)).toBe(true);
    expect(logTicks(0, 10)).toEqual([]);
  });
});

describe("paths", () => {
  it("builds a polyline and rounds coordinates", () => {
    expect(linePath([{ x: 0, y: 1 }, { x: 10.123, y: 2.456 }])).toBe("M0 1L10.12 2.46");
  });
  it("breaks the line at non-finite points", () => {
    expect(linePath([{ x: 0, y: 0 }, { x: 1, y: NaN }, { x: 2, y: 2 }, { x: 3, y: 3 }])).toBe("M0 0 M2 2L3 3");
    expect(linePath([])).toBe("");
  });
  it("builds a closed band along hi then back along lo", () => {
    expect(
      bandPath([
        { x: 0, lo: 10, hi: 5 },
        { x: 10, lo: 12, hi: 4 },
      ])
    ).toBe("M0 5L10 4L10 12L0 10Z");
    expect(bandPath([{ x: 0, lo: 1, hi: 0 }])).toBe("");
  });
});

describe("downsampling", () => {
  it("keeps first and last, evenly spaced", () => {
    expect(downsampleIndices(11, 3)).toEqual([0, 5, 10]);
    expect(downsampleIndices(4, 10)).toEqual([0, 1, 2, 3]);
    expect(downsampleIndices(0, 10)).toEqual([]);
    const idx = downsampleIndices(1000, 50);
    expect(idx).toHaveLength(50);
    expect(idx[0]).toBe(0);
    expect(idx[49]).toBe(999);
  });
  it("downsamples items", () => {
    expect(downsample(["a", "b", "c", "d", "e"], 3)).toEqual(["a", "c", "e"]);
  });
  it("finds the nearest x", () => {
    expect(nearestIndex([1, 10, 100], 40)).toBe(1);
    expect(nearestIndex([1, 10, 100], 80)).toBe(2);
    expect(nearestIndex([1, 10, 100], -3)).toBe(0);
    expect(nearestIndex([], 1)).toBe(-1);
  });
});

describe("formatting", () => {
  it("formats compactly", () => {
    expect(formatCompact(20000)).toBe("20k");
    expect(formatCompact(1500)).toBe("1.5k");
    expect(formatCompact(2_500_000)).toBe("2.5M");
    expect(formatCompact(250)).toBe("250");
    expect(formatCompact(12.5)).toBe("12.5");
    expect(formatCompact(0.0423)).toBe("0.042");
    expect(formatCompact(0)).toBe("0");
    expect(formatCompact(NaN)).toBe("–");
  });
  it("formats percents and integers", () => {
    expect(formatPercent(0.4231)).toBe("42%");
    expect(formatPercent(0.0423, 1)).toBe("4.2%");
    expect(formatInt(20000)).toBe("20,000");
  });
  it("spreads colliding labels apart within bounds", () => {
    const out = spreadLabels([50, 52, 200], 12, 0, 300);
    expect(out[1] - out[0]).toBeGreaterThanOrEqual(12);
    expect(out[2]).toBe(200);
    const clipped = spreadLabels([295, 296, 297], 12, 0, 300);
    expect(Math.max(...clipped)).toBeLessThanOrEqual(300);
    expect(Math.min(...clipped)).toBeGreaterThanOrEqual(0);
  });
});
