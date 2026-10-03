import { describe, expect, it } from "vitest";
import { buildLanes, evenSplit, rankLanes, sharedShareMax, shareY, stripPaths } from "@/lib/scoreboard";
import { ARM_COLORS, type Arm, type CreativeSeries, type ExperimentMetrics, type ExperimentSummary } from "@/lib/experiments";
import liveSummary from "../../scripts/screenshot-fixtures/live-experiment.json";
import liveMetricsJson from "../../scripts/screenshot-fixtures/live-experiment-metrics.json";
import liveCreativesJson from "../../scripts/screenshot-fixtures/live-experiment-creatives.json";

const arms = (liveSummary as unknown as ExperimentSummary).arms as Arm[];
const metrics = liveMetricsJson as unknown as ExperimentMetrics;
const series = liveCreativesJson as unknown as CreativeSeries;

describe("evenSplit", () => {
  it("is 1/K, and 0 with no creatives", () => {
    expect(evenSplit(4)).toBe(0.25);
    expect(evenSplit(3)).toBeCloseTo(1 / 3);
    expect(evenSplit(0)).toBe(0);
  });
});

describe("rankLanes", () => {
  const arm = (index: number) => ({ index }) as Arm;
  it("ranks by final share, unknown last, ties in arm order", () => {
    const ranked = rankLanes([
      { id: "a", finalShare: 0.2, arm: arm(0) },
      { id: "b", finalShare: null, arm: arm(1) },
      { id: "c", finalShare: 0.5, arm: arm(2) },
      { id: "d", finalShare: 0.2, arm: arm(3) },
      { id: "e", finalShare: 0.2, arm: null },
    ]);
    expect(ranked.map((l) => l.id)).toEqual(["c", "a", "d", "e", "b"]);
  });
});

describe("sharedShareMax", () => {
  it("is one scale for every lane: the top share plus headroom, in 10-point steps", () => {
    expect(sharedShareMax([{ share: [0.3, 0.44], finalShare: 0.44 }, { share: [0.2], finalShare: 0.2 }], 3)).toBe(0.5);
    expect(sharedShareMax([{ share: [0.9], finalShare: 0.95 }], 2)).toBe(1);
  });
  it("never sits below the even split, and is 100% with nothing to show", () => {
    expect(sharedShareMax([{ share: [], finalShare: null }, { share: [], finalShare: null }], 2)).toBe(0.6);
    expect(sharedShareMax([], 0)).toBe(1);
  });
  it("ignores non-finite values", () => {
    expect(sharedShareMax([{ share: [Number.NaN, 0.31], finalShare: Number.NaN }], 4)).toBe(0.4);
  });
});

describe("stripPaths / shareY", () => {
  it("maps windows across the width and shares onto the shared scale", () => {
    const { line, area, points } = stripPaths([0, 0.25, 0.5], 0.5, 100, 50);
    expect(points.map((p) => [p.x, p.y])).toEqual([
      [0, 50],
      [50, 25],
      [100, 0],
    ]);
    expect(line).toBe("M0,50L50,25L100,0");
    expect(area).toBe("M0,50L50,25L100,0L100,50L0,50Z");
  });
  it("clamps out-of-range values and skips non-finite ones", () => {
    const { points } = stripPaths([2, Number.NaN, -1], 0.5, 100, 50);
    expect(points.map((p) => p.y)).toEqual([0, 50]);
    expect(stripPaths([], 0.5, 100, 50).line).toBe("");
  });
  it("places the even-split line on the same scale", () => {
    expect(shareY(0.25, 0.5, 56)).toBe(28);
  });
});

describe("buildLanes", () => {
  it("ranks the live creatives by final share with arm colours and observed click rates", () => {
    const lanes = buildLanes(arms, metrics, series);
    expect(lanes.map((l) => [l.rank, l.name])).toEqual([
      [1, "The Tone Dividend Bailout"],
      [2, "The Great Lot Treaty"],
      [3, "Ergonomic Lumbar Relief"],
    ]);
    // Colour follows the creative (arm order), not its rank.
    expect(lanes[1].color).toBe(ARM_COLORS[2]);
    expect(lanes[0].headline).toBe("The Only $500 Check With Real Sustain");
    expect(lanes[0].clickRate).toBeCloseTo(14887 / 322679);
    expect(lanes[0].trueCtr).toBeCloseTo(0.0431913);
    expect(lanes[0].segmentsWon).toEqual(["late_night_casual", "mobile_scrollers"]);
    expect(lanes[0].share).toHaveLength(20);
  });

  it("falls back to the metrics payload without the series", () => {
    const lanes = buildLanes(arms, metrics, null);
    expect(lanes[0].name).toBe("The Tone Dividend Bailout");
    expect(lanes[0].finalShare).toBeCloseTo(0.44, 2);
    expect(lanes[0].clickRate).toBeNull();
    expect(lanes[0].share).toEqual([]);
    expect(lanes[2].segmentsWon).toEqual(["trend_followers"]);
  });

  it("lists every creative before traffic, in arm order, with nothing invented", () => {
    const lanes = buildLanes(arms, null, null);
    expect(lanes.map((l) => l.arm?.index)).toEqual([0, 1, 2]);
    expect(lanes.every((l) => l.finalShare === null && l.clickRate === null && l.trueCtr === null)).toBe(true);
  });
});
