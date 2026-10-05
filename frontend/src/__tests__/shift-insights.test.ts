import { describe, expect, it } from "vitest";
import summary from "../../scripts/screenshot-fixtures/shift-experiment.json";
import metricsJson from "../../scripts/screenshot-fixtures/shift-experiment-metrics.json";
import { buildInsights, ghostGap, recoveryFloor, roundRounds } from "@/lib/experiment-insights";
import { curveSeries, totalBars, type Arm, type ExperimentMetrics } from "@/lib/experiments";
import { runShifts } from "@/lib/shifts";

// A REAL simulator run with two shifts (frontend/scripts/build_shift_fixture.py).
const metrics = metricsJson as unknown as ExperimentMetrics;
const arms = summary.arms as unknown as Arm[];
const run = summary.trafficRuns[1];
const shifts = runShifts(run, metrics.horizon);

const insights = (m: ExperimentMetrics = metrics, extra = {}) =>
  buildInsights({ metrics: m, arms, rewardMode: "click", ctrMode: "demo", scenario: "segment_winners", shifts, forget: true, ...extra });

describe("shift result cards (real simulator run)", () => {
  it("one card per shift, in time order, with episodes and an interval", () => {
    const { shiftCards } = insights();
    expect(shiftCards.map((c) => c.label)).toEqual(["Shift 1", "Shift 2"]);
    expect(shiftCards[0].title).toBe("Demoted The Tone Dividend Bailout for late night casual readers");
    expect(shiftCards[1].title).toBe("Cut Ergonomic Lumbar Relief's clicks by 40%");
    expect(shiftCards[0].status).toBe("recovered");
    expect(shiftCards[0].reading).toMatch(
      /^After you demoted The Tone Dividend Bailout for late night casual readers at round 20,000, the endpoint's best-creative rate fell from 57% to 44% and took 2,600 rounds \(± 1,900\) to recover to 46%\./
    );
    expect(shiftCards[0].reading).toMatch(/UCB, the best baseline, took 2,700\./);
    expect(shiftCards[0].evidence).toBe("10 episodes; ± is a 95% interval across episodes.");
    for (const c of shiftCards) expect(c.reading).not.toMatch(/p\s*[<=]|significan/i);
  });

  it("says too early to call under five episodes, with no claim", () => {
    const thin = structuredClone(metricsJson) as unknown as ExperimentMetrics & { shiftResponse: { policies: Record<string, { episodes: number }> }[] };
    thin.episodes = 3;
    for (const s of thin.shiftResponse) for (const p of Object.values(s.policies)) p.episodes = 3;
    const i = insights(thin);
    expect(i.shiftCards.every((c) => c.status === "too_early")).toBe(true);
    expect(i.shiftCards[0].reading).toBe("Too early to call after 3 episodes: run at least 5 to read how the endpoint responded.");
    expect(i.shiftCards[0].evidence).toBe("3 episodes so far.");
    expect(i.headline).toMatch(/^Too early to call after 3 episodes/);
  });

  it("handles a run that never recovered and one the shift barely dented", () => {
    const m = structuredClone(metricsJson) as unknown as ExperimentMetrics & {
      shiftResponse: { policies: Record<string, Record<string, unknown>> }[];
    };
    const lin0 = m.shiftResponse[0].policies.linear_ts;
    lin0.recoveryRounds = null;
    lin0.recoveredEpisodes = 0;
    const lin1 = m.shiftResponse[1].policies.linear_ts;
    lin1.pctOptimalAfter = { mean: 0.48, lo: 0.45, hi: 0.51, n: 10 };
    lin1.recoveryRounds = { mean: recoveryFloor(40_000), lo: 1000, hi: 1000, n: 10 };
    const i = insights(m);
    expect(i.shiftCards[0].status).toBe("not_recovered");
    expect(i.shiftCards[0].reading).toMatch(/hadn't recovered to 46% by the end of the run\./);
    expect(i.shiftCards[1].status).toBe("held");
    expect(i.shiftCards[1].reading).toMatch(/barely dented it\.$/);
    expect(i.headline).toMatch(/recovered from 1 of 2 shifts/);
  });

  it("no cards (and no shift copy) without shift response numbers", () => {
    const m = { ...metrics, shiftResponse: undefined };
    const i = insights(m);
    expect(i.shiftCards.every((c) => c.status === "no_data")).toBe(true);
    const plain = buildInsights({ metrics: { ...metrics, shiftResponse: undefined }, arms, rewardMode: "click" });
    expect(plain.shiftCards).toEqual([]);
  });
});

describe("shift-aware headline and readings", () => {
  it("names the shifts and how the endpoint responded", () => {
    const i = insights();
    expect(i.headline).toBe("Your endpoint recovered from both shifts, back on the best creative within about 2,600 rounds each time.");
    expect(i.detail).toMatch(/^You demoted The Tone Dividend Bailout for late night casual readers and cut Ergonomic Lumbar Relief's clicks by 40%, at 50% and 60% of the run\./);
  });

  it("states the ghost gap with an interval", () => {
    const gg = ghostGap(metrics)!;
    expect(gg.diff).toBeCloseTo(113.6, 1);
    expect(gg.half).toBeGreaterThan(0);
    expect(insights().ghost).toMatch(/^Without your shifts, the same endpoint would have earned 114 \(± \d+\) more clicks per episode/);
  });

  it("never treats the ghost as a baseline to beat", () => {
    const i = insights();
    expect(i.readings.totals).toMatch(/against 1,563 ± 29 for UCB/);
    expect(i.readings.avgReward).toMatch(/Without your shifts \(dashed\) it would have ended at 4\.3%\./);
  });

  it("reads segments from the last period, not the whole run", () => {
    expect(insights().readings.segments).toMatch(/^In the last period of the run, /);
  });

  it("generalises the trailing explanation to shifts", () => {
    const m = structuredClone(metricsJson) as unknown as ExperimentMetrics;
    m.totals.ucb1 = { mean: 1800, std: 10 };
    m.curves.ucb1.cumAvgReward.lo = m.curves.ucb1.cumAvgReward.mean.map(() => 0.06);
    m.curves.ucb1.cumAvgReward.hi = m.curves.ucb1.cumAvgReward.mean.map(() => 0.07);
    m.curves.ucb1.cumAvgReward.mean = m.curves.ucb1.cumAvgReward.mean.map(() => 0.065);
    const i = insights(m);
    expect(i.verdict).toBe("behind");
    expect(i.headline).toMatch(/your shifts changed what readers want at 50% and 60% of the run/);
    expect(i.why).toMatch(/remembering roughly the last 5,000 readers, so it does recover after each shift/);
  });
});

describe("chart series with the ghost", () => {
  it("draws the ghost dashed, without a band, next to Linear TS", () => {
    const s = curveSeries(metrics, "cumRegret", { bands: true });
    expect(s.map((x) => x.id).slice(0, 2)).toEqual(["linear_ts", "linear_ts_unshifted"]);
    const ghost = s[1];
    expect(ghost.label).toBe("Linear TS without your shifts");
    expect(ghost.dash).toBeTruthy();
    expect(ghost.band).toBeUndefined();
  });

  it("adds a total-reward bar for the ghost", () => {
    expect(totalBars(metrics).map((b) => b.label)).toContain("Linear TS without your shifts");
  });

  it("rounds round counts to two significant figures", () => {
    expect(roundRounds(2634.1)).toBe(2600);
    expect(roundRounds(1057)).toBe(1100);
    expect(roundRounds(710.9)).toBe(710);
  });
});
