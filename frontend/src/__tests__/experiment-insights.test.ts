import { describe, expect, it } from "vitest";
import {
  bandsOverlap,
  bestBaseline,
  buildInsights,
  joinList,
  relativeChange,
  verdictOf,
  type ExperimentInsights,
} from "@/lib/experiment-insights";
import type { Arm, CreativeSeries, ExperimentMetrics, ExperimentSummary } from "@/lib/experiments";
import liveSummary from "../../scripts/screenshot-fixtures/live-experiment.json";
import liveMetricsJson from "../../scripts/screenshot-fixtures/live-experiment-metrics.json";
import liveCreativesJson from "../../scripts/screenshot-fixtures/live-experiment-creatives.json";

const liveArms = (liveSummary as unknown as ExperimentSummary).arms as Arm[];
const liveMetrics = liveMetricsJson as unknown as ExperimentMetrics;
const liveSeries = liveCreativesJson as unknown as CreativeSeries;

const band = (mean: number, half: number) => ({ mean: [mean], lo: [mean - half], hi: [mean + half] });

function arm(id: string, index: number, conceptName: string): Arm {
  return { creativeId: id, index, label: `${conceptName} headline`, conceptName, imageUri: null, scores: {}, overallScore: null };
}
const ARMS = [arm("a1", 0, "Alpha"), arm("b2", 1, "Bravo")];

/** A two-policy-plus-oracle payload with controllable linear_ts and ucb1 rewards/bands. */
function metricsWith({
  lin = 0.05,
  base = 0.04,
  half = 0.001,
  episodes = 20,
}: { lin?: number; base?: number; half?: number; episodes?: number } = {}): ExperimentMetrics {
  return {
    experimentId: "exp-1",
    episodes,
    horizon: 10_000,
    checkpoints: [10_000],
    policies: ["linear_ts", "ucb1", "uniform", "oracle"],
    curves: {
      linear_ts: { cumAvgReward: band(lin, half), cumRegret: band(100, 5), pctOptimal: band(0.7, 0.02) },
      ucb1: { cumAvgReward: band(base, half), cumRegret: band(200, 5), pctOptimal: band(0.5, 0.02) },
      uniform: { cumAvgReward: band(0.03, half), cumRegret: band(300, 1), pctOptimal: band(0.5, 0.001) },
      oracle: { cumAvgReward: band(0.06, 0), cumRegret: band(0, 0), pctOptimal: band(1, 0) },
    },
    totals: {
      linear_ts: { mean: lin * 10_000, std: 12 },
      ucb1: { mean: base * 10_000, std: 10 },
      uniform: { mean: 300, std: 5 },
      oracle: { mean: 600, std: 8 },
    },
    armShare: { a1: [0.5, 0.8], b2: [0.5, 0.2] },
    perSegment: {
      young: { optimalArm: "a1", policies: { linear_ts: { pctOptimal: 0.8, avgReward: 0.05 }, ucb1: { pctOptimal: 0.6, avgReward: 0.04 } } },
    },
    arms: [
      { creativeId: "a1", impressions: 8000, estimatedCtr: 0.051, trueCtr: 0.05 },
      { creativeId: "b2", impressions: 2000, estimatedCtr: 0.031, trueCtr: 0.03 },
    ],
  };
}

const allText = (i: ExperimentInsights) =>
  [i.headline, i.support, ...Object.values(i.readings), ...Object.values(i.lanes)].filter(Boolean).join("\n");

describe("helpers", () => {
  it("joins lists in plain English", () => {
    expect(joinList([])).toBe("");
    expect(joinList(["a"])).toBe("a");
    expect(joinList(["a", "b"])).toBe("a and b");
    expect(joinList(["a", "b", "c"])).toBe("a, b and c");
  });
  it("computes relative change and refuses undefined ones", () => {
    expect(relativeChange(110, 100)).toBeCloseTo(0.1);
    expect(relativeChange(90, 100)).toBeCloseTo(-0.1);
    expect(relativeChange(1, 0)).toBeNull();
    expect(relativeChange(Number.NaN, 1)).toBeNull();
  });
  it("treats missing bounds as overlapping", () => {
    expect(bandsOverlap({ lo: 1, hi: 2 }, { lo: 3, hi: 4 })).toBe(false);
    expect(bandsOverlap({ lo: 1, hi: 3 }, { lo: 2, hi: 4 })).toBe(true);
    expect(bandsOverlap({ lo: null, hi: 2 }, { lo: 3, hi: 4 })).toBe(true);
  });
  it("picks the best baseline by total reward, never the endpoint or oracle", () => {
    expect(bestBaseline(liveMetrics)).toBe("ucb1");
    expect(bestBaseline(metricsWith())).toBe("ucb1");
  });
});

describe("verdict and headline", () => {
  it("claims a win only when the bands clear", () => {
    const i = buildInsights({ metrics: metricsWith(), arms: ARMS, rewardMode: "click" });
    expect(i.verdict).toBe("ahead");
    expect(i.headline).toBe(
      "Your endpoint learned to favour Alpha for every reader group, earning 25.0% more clicks than the best baseline strategy."
    );
  });

  it("says too early when the 95% bands overlap", () => {
    const m = metricsWith({ lin: 0.041, base: 0.04, half: 0.002 });
    expect(verdictOf(m)).toBe("too_early");
    const i = buildInsights({ metrics: m, arms: ARMS });
    expect(i.headline).toMatch(/^Too early to call: Alpha leads on traffic, but the endpoint and the best baseline/);
    expect(i.headline).not.toMatch(/earning/);
    expect(i.support).toContain("Run more episodes");
    expect(i.readings.totals).toContain("within the margin of error");
  });

  it("says too early with fewer than five episodes even if the bands clear", () => {
    const i = buildInsights({ metrics: metricsWith({ episodes: 3 }), arms: ARMS });
    expect(i.verdict).toBe("too_early");
    expect(i.headline).toContain("after 3 episodes");
  });

  it("admits when the best baseline is ahead", () => {
    const i = buildInsights({ metrics: metricsWith({ lin: 0.03, base: 0.04 }), arms: ARMS });
    expect(i.verdict).toBe("behind");
    expect(i.headline).toBe("The best baseline strategy, UCB, is earning 33.3% more clicks than your endpoint so far.");
  });

  it("is empty with no episodes", () => {
    const i = buildInsights({ metrics: { ...metricsWith(), episodes: 0, checkpoints: [] }, arms: ARMS });
    expect(i).toMatchObject({ verdict: "empty", headline: "", support: "", lanes: {} });
    expect(buildInsights({ metrics: null, arms: ARMS }).verdict).toBe("empty");
  });

  it("uses engaged-time wording in engaged mode", () => {
    const i = buildInsights({ metrics: metricsWith(), arms: ARMS, rewardMode: "engaged" });
    expect(i.headline).toContain("more engaged time than");
    expect(i.readings.totals).toContain("engaged seconds per episode");
  });
});

describe("the first live experiment", () => {
  const i = buildInsights({ metrics: liveMetrics, series: liveSeries, arms: liveArms, rewardMode: "click", ctrMode: "demo" });

  it("headlines the segment winner and the gain over the best baseline", () => {
    expect(i.verdict).toBe("ahead");
    expect(i.headline).toBe(
      "Your endpoint learned to favour The Tone Dividend Bailout for late night casual and mobile scrollers, earning 5.6% more clicks than the best baseline strategy."
    );
    expect(i.detail).toBe(
      "Product intenders see The Great Lot Treaty; trend followers see Ergonomic Lumbar Relief."
    );
    expect(i.support).toMatch(/^20 episodes/);
    expect(i.support).toContain("20 episodes of 40,000 simulated readers each");
    expect(i.support).toContain("Demo click rates run high");
  });

  it("reads every chart with the payload's numbers", () => {
    expect(i.readings.avgReward).toBe(
      "Linear TS ends at 4.4% against the oracle's 5.1%, closing 26% of the gap between the best baseline (UCB, 4.2%) and that ceiling."
    );
    expect(i.readings.regret).toBe(
      "Linear TS lost 257 clicks against the oracle by round 40,000, 23% fewer than the best baseline, ε-greedy (335)."
    );
    expect(i.readings.optimal).toBe(
      "Linear TS showed each reader their best creative 56% of the time by the end, 14 points ahead of ε-greedy (42%)."
    );
    expect(i.readings.share).toMatch(/^Traffic moved toward The Tone Dividend Bailout, from 35% to 45% of impressions and away from Ergonomic Lumbar Relief, from 34% to 25%\.$/);
    expect(i.readings.segments).toBe(
      "The Tone Dividend Bailout wins late night casual and mobile scrollers; The Great Lot Treaty wins product intenders; Ergonomic Lumbar Relief wins trend followers. Linear TS found the winner more often than every baseline in 2 of 4 segments (product intenders and trend followers)."
    );
    expect(i.readings.totals).toBe(
      "Linear TS collects 1,760 ± 58 clicks per episode against 1,667 ± 28 for UCB: 93 more (+5.6%)."
    );
  });

  it("reads each creative lane", () => {
    expect(i.lanes.aae3f6b4).toBe(
      "The Tone Dividend Bailout ends with 45% of the endpoint's traffic, up from 35% at the start. Readers clicked it 4.6% of the time against a true rate of 4.3%. It is the best creative for late night casual and mobile scrollers."
    );
    expect(i.lanes["8c0e9ee8"]).toContain("down from 34% at the start");
  });

  it("never prints NaN, Infinity or undefined", () => {
    expect(allText(i)).not.toMatch(/NaN|Infinity|undefined|null/);
  });
});

describe("partial payloads", () => {
  it("falls back to metrics when the creative series is missing", () => {
    const i = buildInsights({ metrics: liveMetrics, series: null, arms: liveArms });
    expect(i.readings.share).toBe(
      "By the end, The Tone Dividend Bailout gets 44% of impressions and Ergonomic Lumbar Relief 25%, against an even split of 33%."
    );
    expect(i.lanes.aae3f6b4).toContain("ends with 44% of the endpoint's traffic.");
    expect(i.lanes.aae3f6b4).toContain("true click rate in the simulator is 4.3%");
  });

  it("returns null readings rather than NaN when curves are missing", () => {
    const m = { ...metricsWith(), curves: {}, totals: {}, perSegment: {} } as ExperimentMetrics;
    const i = buildInsights({ metrics: m, arms: ARMS });
    expect(i.verdict).toBe("too_early");
    expect(i.readings).toMatchObject({ avgReward: null, regret: null, optimal: null, segments: null, totals: null });
    expect(allText(i)).not.toMatch(/NaN|Infinity|undefined/);
  });

  it("survives non-finite numbers in the payload", () => {
    const m = metricsWith();
    m.curves.linear_ts.cumRegret = { mean: [Number.NaN], lo: [Number.NaN], hi: [Number.NaN] };
    m.totals.ucb1 = { mean: Number.NaN, std: Number.NaN };
    const i = buildInsights({ metrics: m, arms: ARMS });
    expect(i.readings.regret).toBeNull();
    expect(allText(i)).not.toMatch(/NaN|Infinity|undefined/);
  });
});
