import { describe, expect, it } from "vitest";
import {
  bandsOverlap,
  bestBaseline,
  forgetsEvidence,
  memoryReaders,
  swapWhen,
  buildInsights,
  joinList,
  relativeChange,
  setupNotes,
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

  it("leaves `why` empty when the endpoint leads", () => {
    const i = buildInsights({ metrics: metricsWith(), arms: ARMS, scenario: "drift" });
    expect(i.verdict).toBe("ahead");
    expect(i).toMatchObject({ why: "", whyExplain: "" });
  });

  it("explains a generic shortfall by the cost of learning reader context", () => {
    const i = buildInsights({ metrics: metricsWith({ lin: 0.03, base: 0.04 }), arms: ARMS, scenario: "clear_winner" });
    expect(i.why).toBe(
      "UCB earned 33.3% more clicks per episode than your endpoint. In this scenario one creative is best for every reader, so knowing the reader adds nothing, and the endpoint pays for estimating 19 reader features per creative. UCB only tracks one click rate per creative, so it settles on the winner sooner."
    );
    expect(i.whyExplain).toMatch(/^Linear TS learns how each reader's context/);
    // the headline itself is unchanged outside drift
    expect(i.headline).toBe("The best baseline strategy, UCB, is earning 33.3% more clicks than your endpoint so far.");
  });

  it("explains a trailing endpoint even while the bands still overlap", () => {
    const i = buildInsights({
      metrics: metricsWith({ lin: 0.039, base: 0.04, half: 0.01 }),
      arms: ARMS,
      scenario: "segment_winners",
    });
    expect(i.verdict).toBe("too_early");
    expect(i.why).toContain("needs more traffic before its reader-by-reader choices pay off");
  });
});

describe("drift readings", () => {
  // Experiment 2a7685cf9c8d4d3e (2026-10-05, drift, demo, 4 arms, 20 × 40k): per-episode
  // reward oracle 2597, UCB 2394, BB-TS 2233, ε-greedy 2170, linear_ts 2048, uniform 1870.
  function driftMetrics(): ExperimentMetrics {
    const m = metricsWith({ lin: 0.0512, base: 0.05985 });
    return {
      ...m,
      horizon: 40_000,
      totals: {
        linear_ts: { mean: 2047.6, std: 40 },
        ucb1: { mean: 2394.0, std: 60 },
        beta_bernoulli_ts: { mean: 2233.4, std: 70 },
        epsilon_greedy: { mean: 2170.0, std: 80 },
        uniform: { mean: 1869.6, std: 20 },
        oracle: { mean: 2597.2, std: 30 },
      },
    };
  }

  it("says why linear TS trailed UCB for a full-memory endpoint", () => {
    const i = buildInsights({ metrics: driftMetrics(), arms: ARMS, scenario: "drift", policyDiscount: 1 });
    expect(i.verdict).toBe("behind");
    expect(i.headline).toBe(
      "Linear TS trailed UCB here: the best and worst creatives swap halfway through, and an endpoint that weighs old evidence fully adapts slowly."
    );
    expect(i.why).toBe(
      "UCB earned 16.9% more clicks per episode than your endpoint. In this scenario the best and worst creatives swap halfway through the run. This endpoint weighs every past reader as heavily as the latest one, so after the swap it keeps backing the old winner until the new evidence outweighs the old. UCB keeps re-checking the creatives it has shown least, so it spots the new winner sooner."
    );
    expect(i.whyExplain).toMatch(/Forgetting old evidence \(a discount\) shortens that memory/);
    expect(i.why).not.toMatch(/on purpose/);
  });

  it("treats a missing discount (older experiments) as full memory", () => {
    const i = buildInsights({ metrics: driftMetrics(), arms: ARMS, scenario: "drift" });
    expect(i.why).toContain("weighs every past reader as heavily as the latest one");
  });

  it("mentions that a discounted endpoint forgets on purpose", () => {
    const i = buildInsights({ metrics: driftMetrics(), arms: ARMS, scenario: "drift", policyDiscount: 0.98 });
    expect(i.headline).toBe(
      "Linear TS trailed UCB here: the best and worst creatives swap halfway through, and even an endpoint that forgets old evidence has more to re-learn than UCB."
    );
    expect(i.why).toContain(
      "This endpoint forgets old evidence on purpose: it weighs each batch of readers a little less than the next, remembering roughly the last 5,000 readers, so it does recover after the swap."
    );
    expect(i.why).toContain("19 reader features per creative to re-learn");
  });

  it("uses the tuned change point", () => {
    const i = buildInsights({
      metrics: driftMetrics(),
      arms: ARMS,
      scenario: "drift",
      scenarioOverrides: { driftAtFrac: 0.3 },
    });
    expect(i.headline).toContain("swap 30% of the way through,");
    expect(i.why).toContain("swap 30% of the way through the run.");
  });

  it("names what lets the baseline adapt when it isn't UCB", () => {
    const m = driftMetrics();
    const { ucb1: _drop, ...totals } = m.totals;
    const curves = { ...m.curves };
    delete curves.ucb1;
    const i = buildInsights({
      metrics: { ...m, totals, curves, policies: m.policies.filter((p) => p !== "ucb1") },
      arms: ARMS,
      scenario: "drift",
    });
    expect(i.why).toMatch(/^TS \(no context\) earned 9\.1% more clicks/);
    expect(i.why).toContain("tracks one click rate per creative, so it has far less to re-learn");
  });
});

describe("discount helpers", () => {
  it("knows when an endpoint forgets and roughly how much it remembers", () => {
    expect(forgetsEvidence(1)).toBe(false);
    expect(forgetsEvidence(undefined)).toBe(false);
    expect(forgetsEvidence(0.98)).toBe(true);
    expect(memoryReaders(0.98)).toBe(5000); // 100 / -ln 0.98 ≈ 4950
    expect(memoryReaders(0.998)).toBe(50_000);
    expect(memoryReaders(1)).toBeNull();
  });
  it("phrases the drift change point", () => {
    expect(swapWhen(undefined)).toBe("halfway through");
    expect(swapWhen(0.5)).toBe("halfway through");
    expect(swapWhen(0.7)).toBe("70% of the way through");
  });
});

describe("empty and engaged", () => {
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

describe("tuned-reader notes (contracts §9)", () => {
  it("are empty for preset experiments", () => {
    expect(setupNotes("segment_winners", null)).toEqual([]);
    expect(setupNotes("segment_winners", { gapScale: 1.5, judgeWrong: 0.3 })).toEqual([]);
    expect(buildInsights({ metrics: null, arms: ARMS }).notes).toEqual([]);
  });

  it("say when the judge was set to mislead", () => {
    const [backwards] = setupNotes("clear_winner", { judgeWrong: 1 });
    expect(backwards).toMatch(/^The eval judge was set to mislead; |^The eval judge was set to mislead: /);
    expect(backwards).toMatch(/learn against the scores/);
    expect(setupNotes("clear_winner", { judgeWrong: 0.7 })[0]).toMatch(/set to mislead/);
    expect(setupNotes("clear_winner", { judgeWrong: 0.5 })).toEqual([]);
  });

  it("say when the audience mix is skewed, naming the dominant segment", () => {
    const notes = setupNotes("segment_winners", { segmentMix: [0.55, 0.05, 0.3, 0.1] });
    expect(notes).toEqual([
      "Most simulated readers were mobile scrollers (55%), so the overall results lean toward what that segment prefers.",
    ]);
    expect(setupNotes("segment_winners", { segmentMix: [0.3, 0.25, 0.25, 0.2] })).toEqual([]);
  });

  it("reach buildInsights even before there are results", () => {
    const i = buildInsights({
      metrics: null,
      arms: ARMS,
      scenario: "segment_winners",
      scenarioOverrides: { judgeWrong: 1, segmentMix: [0.55, 0.05, 0.3, 0.1] },
    });
    expect(i.verdict).toBe("empty");
    expect(i.notes).toHaveLength(2);
  });
});
