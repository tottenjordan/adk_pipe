import { describe, expect, it } from "vitest";
import {
  bestBySegment,
  buildCreativeDetail,
  cellShade,
  clickTrafficShare,
  formatSignedPercent,
  liftVsSet,
  pooledCtr,
  readingSentence,
  segmentGrid,
  segmentScaleMax,
  segmentWords,
} from "@/lib/creative-detail";
import { parseCreativeParam, urlForCreative } from "@/lib/experiment-view";
import type { CreativeSegmentStat, CreativeSeries, CreativeSeriesItem } from "@/lib/experiments";
import liveCreativesJson from "../../scripts/screenshot-fixtures/live-experiment-creatives.json";

const live = liveCreativesJson as unknown as CreativeSeries;
const byId = (id: string) => live.creatives.find((c) => c.creativeId === id) as CreativeSeriesItem;

/** The same series as the older API sent it: no segments / missedClicks / engagedSecondsPer1k. */
const legacy: CreativeSeries = {
  ...live,
  creatives: live.creatives.map((c) => {
    const { segments: _s, missedClicks: _m, engagedSecondsPer1k: _e, ...rest } = c;
    return rest;
  }),
};

const item = (over: Partial<CreativeSeriesItem> = {}): CreativeSeriesItem => ({
  creativeId: "x",
  share: [],
  ctr: [],
  cumClicks: [],
  impressions: 0,
  clicks: 0,
  trueCtr: null,
  segmentsWon: [],
  finalShare: 0,
  ...over,
});

const seg = (segment: string, impressions: number, clicks: number, isBest = false): CreativeSegmentStat => ({
  segment,
  impressions,
  clicks,
  ctr: impressions ? clicks / impressions : null,
  trueCtr: null,
  isBest,
});

describe("pooledCtr / liftVsSet", () => {
  it("pools clicks over impressions across every creative", () => {
    // 14887 + 11434 + 8882 clicks over 322679 + 263016 + 214305 impressions.
    expect(pooledCtr(live)).toBeCloseTo(35203 / 800000, 10);
  });

  it("is this creative's rate over the pooled rate, minus one", () => {
    const lift = liftVsSet(byId("aae3f6b4"), live) as number;
    expect(lift).toBeCloseTo(14887 / 322679 / (35203 / 800000) - 1, 10);
    expect(lift).toBeGreaterThan(0.04);
    expect(liftVsSet(byId("8c0e9ee8"), live)).toBeLessThan(0);
  });

  it("is null without impressions, without a series, or with a single creative", () => {
    expect(liftVsSet(item(), live)).toBeNull();
    expect(liftVsSet(byId("aae3f6b4"), null)).toBeNull();
    const solo = { creatives: [byId("aae3f6b4")] };
    expect(liftVsSet(byId("aae3f6b4"), solo)).toBeNull();
    expect(pooledCtr({ creatives: [] })).toBeNull();
  });
});

describe("clickTrafficShare", () => {
  it("compares the share of all impressions with the share of all clicks", () => {
    const s = clickTrafficShare(byId("aae3f6b4"), live);
    expect(s.traffic).toBeCloseTo(322679 / 800000, 10);
    expect(s.clicks).toBeCloseTo(14887 / 35203, 10);
    expect(s.clicks as number).toBeGreaterThan(s.traffic as number);
  });

  it("is null for each side with no totals", () => {
    expect(clickTrafficShare(item(), { creatives: [item()] })).toEqual({ traffic: null, clicks: null });
    expect(clickTrafficShare(null, live)).toEqual({ traffic: null, clicks: null });
  });
});

describe("segmentScaleMax", () => {
  it("covers the highest observed or true segment rate across every creative, on a nice step", () => {
    // Highest: 3fe4b3ec × product_intenders, 6423 / 122474 = 5.24% (true 5.09%).
    expect(segmentScaleMax(live)).toBeCloseTo(0.06, 10);
  });

  it("uses the true rate when it is the larger value, and is 0 without segments", () => {
    const s = { creatives: [item({ segments: [{ ...seg("a", 1000, 40), trueCtr: 0.09 }] })] };
    expect(segmentScaleMax(s)).toBeGreaterThanOrEqual(0.09);
    expect(segmentScaleMax(legacy)).toBe(0);
    expect(segmentScaleMax(null)).toBe(0);
  });
});

describe("bestBySegment", () => {
  it("maps each segment to the creative marked best", () => {
    expect(bestBySegment(live)).toEqual({
      late_night_casual: "aae3f6b4",
      mobile_scrollers: "aae3f6b4",
      product_intenders: "3fe4b3ec",
      trend_followers: "8c0e9ee8",
    });
    expect(bestBySegment(legacy)).toEqual({});
  });
});

describe("readingSentence", () => {
  it("names the top segment against its rate elsewhere, and counts best picks (live data)", () => {
    // mobile scrollers 5.22% vs (14887 − 5764) / (322679 − 110336) = 4.30% elsewhere → +21%.
    expect(readingSentence(byId("aae3f6b4"), live)).toBe(
      "Earns 5.2% with mobile scrollers, 21% above its rate with other readers; it was the right pick for 2 of 4 segments."
    );
    expect(readingSentence(byId("3fe4b3ec"), live)).toMatch(
      /^Earns 5\.2% with product intenders, \d+% above its rate with other readers; it was the right pick for 1 of 4 segments\.$/
    );
  });

  it("says when rates are about the same across segments, and when no segment was won", () => {
    const flat = item({
      impressions: 4000,
      clicks: 200,
      segments: [seg("a", 2000, 100), seg("b", 2000, 102)],
    });
    expect(readingSentence(flat, null)).toBe(
      "Earns about the same with every segment (5.0% to 5.1%); it was not the best pick for any of the 2 segments."
    );
  });

  it("says when it won every segment", () => {
    const all = item({
      impressions: 4000,
      clicks: 300,
      segments: [seg("a", 2000, 200, true), seg("b", 2000, 100, true)],
    });
    expect(readingSentence(all, null)).toMatch(/it was the right pick for all 2 segments\.$/);
  });

  it("is honest when the data is thin", () => {
    expect(readingSentence(item(), live)).toBe("No readers have seen this creative yet.");
    expect(readingSentence(null, live)).toBe("No readers have seen this creative yet.");
    expect(readingSentence(item({ impressions: 900, clicks: 12 }), live)).toBe(
      "Only 12 clicks from 900 impressions so far: too few to say how this creative performs."
    );
    expect(readingSentence(item({ impressions: 40, clicks: 1 }), live)).toMatch(/^Only 1 click from 40/);
  });

  it("does not compare segments that each have too few clicks", () => {
    const sparse = item({
      impressions: 2000,
      clicks: 40,
      segments: [seg("a", 400, 10), seg("b", 400, 10), seg("c", 1200, 20, true)],
    });
    expect(readingSentence(sparse, null)).toBe(
      "Earns 2.0% overall, with too few clicks per segment to compare them; it was the right pick for 1 of 3 segments."
    );
  });

  it("falls back to the set comparison and segmentsWon when segments are missing", () => {
    expect(readingSentence(legacy.creatives.find((c) => c.creativeId === "aae3f6b4"), legacy)).toBe(
      "Earns 4.6% overall, 5% above the pooled rate of all creatives; it is the best choice for 2 segments."
    );
    expect(readingSentence(legacy.creatives.find((c) => c.creativeId === "8c0e9ee8"), legacy)).toMatch(
      /^Earns 4\.1% overall, 6% below the pooled rate of all creatives; it is the best choice for 1 segment\.$/
    );
    const alone = item({ impressions: 1000, clicks: 50 });
    expect(readingSentence(alone, { creatives: [alone] })).toBe("Earns 5.0% overall.");
  });
});

describe("buildCreativeDetail", () => {
  it("assembles the drawer's numbers from the live fixture", () => {
    const d = buildCreativeDetail(byId("aae3f6b4"), live);
    expect(d).not.toBeNull();
    if (!d) return;
    expect(d.impressions).toBe(322679);
    expect(d.clicks).toBe(14887);
    expect(d.missedClicks).toBe(67.35);
    expect(d.clicksPerEpisode).toBeCloseTo(14887 / 20, 6);
    expect(d.engagedSecondsPer1k).toBeNull();
    expect(d.bestCount).toBe(2);
    expect(d.segments.map((s) => s.segment)).toEqual([
      "late_night_casual",
      "mobile_scrollers",
      "product_intenders",
      "trend_followers",
    ]);
    expect(d.segments[2]).toMatchObject({ isBest: false, bestCreativeId: "3fe4b3ec", trueCtr: 0.0367 });
    expect(d.ctrPoints).toHaveLength(20);
    expect(d.ctrPoints[0].x).toBe(1000); // mid round of the first window [0, 2000)
    expect(d.segmentMax).toBeCloseTo(0.06, 10);
    expect(d.thin).toBe(false);
  });

  it("leaves the new fields null/empty on an older API, so the drawer hides those sections", () => {
    const d = buildCreativeDetail(legacy.creatives[0], legacy);
    expect(d?.segments).toEqual([]);
    expect(d?.missedClicks).toBeNull();
    expect(d?.engagedSecondsPer1k).toBeNull();
    expect(d?.segmentMax).toBe(0);
    expect(d?.bestCount).toBe(2); // from segmentsWon
    expect(d?.lift).not.toBeNull();
  });

  it("drops null click-rate windows, recomputes a null segment ctr, and keeps engaged seconds", () => {
    const c = item({
      impressions: 100,
      clicks: 5,
      ctr: [null, 0.05, null],
      engagedSecondsPer1k: 812.5,
      segments: [{ ...seg("a", 100, 5), ctr: null }],
    });
    const d = buildCreativeDetail(c, { creatives: [c], windows: [], episodes: 0 });
    expect(d?.ctrPoints).toEqual([{ x: 1, y: 0.05 }]);
    expect(d?.segments[0].ctr).toBeCloseTo(0.05);
    expect(d?.engagedSecondsPer1k).toBe(812.5);
    expect(d?.clicksPerEpisode).toBeNull();
    expect(d?.thin).toBe(true);
    expect(buildCreativeDetail(null, live)).toBeNull();
  });
});

describe("segmentGrid / cellShade", () => {
  it("orders rows like the scoreboard and spans the observed range", () => {
    const g = segmentGrid(live, ["aae3f6b4", "3fe4b3ec", "8c0e9ee8"]);
    expect(g?.rows.map((r) => r.creativeId)).toEqual(["aae3f6b4", "3fe4b3ec", "8c0e9ee8"]);
    expect(g?.segments).toHaveLength(4);
    expect(g?.range?.[0]).toBeCloseTo(901 / 29667, 4);
    expect(g?.range?.[1]).toBeCloseTo(6423 / 122474, 4); // 3fe4b3ec × product intenders
    const best = g?.rows.flatMap((r) => r.cells.filter((c) => c.isBest).map((c) => `${r.creativeId}:${c.segment}`));
    expect(best).toHaveLength(4);
  });

  it("is null without segment data", () => {
    expect(segmentGrid(legacy)).toBeNull();
    expect(segmentGrid(null)).toBeNull();
  });

  it("shades 0 at the lowest rate, 1 at the highest, 0.5 when flat", () => {
    expect(cellShade(0.03, [0.03, 0.05])).toBe(0);
    expect(cellShade(0.05, [0.03, 0.05])).toBe(1);
    expect(cellShade(0.04, [0.03, 0.05])).toBeCloseTo(0.5);
    expect(cellShade(0.04, [0.04, 0.04])).toBe(0.5);
    expect(cellShade(null, [0.03, 0.05])).toBe(0);
  });
});

describe("formatting helpers", () => {
  it("formats signed percents with a true minus", () => {
    expect(formatSignedPercent(0.0481)).toBe("+4.8%");
    expect(formatSignedPercent(-0.125)).toBe("−13%");
    expect(formatSignedPercent(0.00001)).toBe("0%");
  });

  it("turns segment keys into words", () => {
    expect(segmentWords("late_night_casual")).toBe("late night casual");
  });
});

describe("?creative= deep link", () => {
  it("parses ids and keeps ?view= when setting or clearing", () => {
    expect(parseCreativeParam("aae3f6b4")).toBe("aae3f6b4");
    expect(parseCreativeParam("")).toBeNull();
    expect(parseCreativeParam("../x")).toBeNull();
    expect(urlForCreative("http://h/experiments/e?view=analysis", "abc")).toBe(
      "/experiments/e?view=analysis&creative=abc"
    );
    expect(urlForCreative("http://h/experiments/e?view=analysis&creative=abc", null)).toBe(
      "/experiments/e?view=analysis"
    );
  });
});
