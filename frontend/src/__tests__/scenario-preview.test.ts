import { describe, expect, it } from "vitest";
import golden from "./fixtures/scenario-preview-golden.json";
import type { Scenario, ScenarioOverrides } from "@/lib/experiments";
import {
  armOverall,
  armScoresFromProof,
  displayPercents,
  formatPts,
  MIX_FLOOR,
  presetValues,
  previewMatrix,
  previewReading,
  rebalanceMix,
  SCENARIO_PRESETS,
  spreadRankCtrs,
  valuesFromOverrides,
} from "@/lib/scenario-preview";
import type { Proof } from "@/lib/eval-matching";

/** ±0.2 percentage points, the parity bound the plan asks for. */
const PARITY_TOL = 0.002;
/** What the exact-expectation port actually achieves (α's Monte Carlo in bandit is the only gap). */
const ACHIEVED_TOL = 0.00001; // max seen 6.9e-6 = 0.0007 pts

type GoldenCase = {
  name: string;
  scenario: Scenario;
  ctrMode: "demo" | "realistic";
  arms: Record<string, number>[];
  overrides: ScenarioOverrides;
  expected: { segments: string[]; ctr: number[][]; oracle: number[]; overall: number[]; alpha: number };
};
const cases = (golden as { cases: GoldenCase[] }).cases;

const run = (c: GoldenCase) =>
  previewMatrix({
    scenario: c.scenario,
    ctrMode: c.ctrMode,
    arms: c.arms,
    values: valuesFromOverrides(c.scenario, c.overrides),
  });

describe("previewMatrix golden parity with bandit (tests/test_scenario_preview_golden.py)", () => {
  it("covers every scenario and several override combinations", () => {
    expect(new Set(cases.map((c) => c.scenario))).toEqual(new Set(["clear_winner", "segment_winners", "drift"]));
    expect(cases.length).toBeGreaterThanOrEqual(12);
  });

  it.each(cases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const pv = run(c);
    expect(pv.segments).toEqual(c.expected.segments);
    expect(pv.oracle).toEqual(c.expected.oracle);
    expect(pv.ctr.length).toBe(c.arms.length);
    pv.ctr.forEach((row, a) =>
      row.forEach((v, s) => {
        const want = c.expected.ctr[a][s];
        expect(Math.abs(v - want), `${c.name} creative ${a} segment ${s}`).toBeLessThanOrEqual(PARITY_TOL);
        expect(Math.abs(v - want)).toBeLessThanOrEqual(ACHIEVED_TOL);
      })
    );
    pv.overall.forEach((v, a) => expect(Math.abs(v - c.expected.overall[a])).toBeLessThanOrEqual(ACHIEVED_TOL));
  });
});

describe("previewMatrix behaviour", () => {
  const arms = cases.find((c) => c.name === "clear_winner/three/preset")!.arms;

  it("calibrates the mean click rate to the scenario target", () => {
    for (const scenario of ["clear_winner", "segment_winners"] as const) {
      const pv = previewMatrix({ scenario, ctrMode: "demo", arms, values: presetValues(scenario) });
      const meanRate = pv.overall.reduce((a, b) => a + b, 0) / pv.overall.length;
      expect(meanRate).toBeCloseTo(SCENARIO_PRESETS[scenario].targetCtr.demo, 6);
    }
  });

  it("judge backwards flips clear_winner's ranking, and a wider gap widens it", () => {
    const right = previewMatrix({ scenario: "clear_winner", ctrMode: "demo", arms, values: presetValues("clear_winner") });
    const back = previewMatrix({
      scenario: "clear_winner",
      ctrMode: "demo",
      arms,
      values: { ...presetValues("clear_winner"), judgeWrong: 1 },
    });
    expect(right.bestOverall).toBe(right.judgeFavourite);
    expect(back.bestOverall).not.toBe(back.judgeFavourite);
    const wide = previewMatrix({
      scenario: "clear_winner",
      ctrMode: "demo",
      arms,
      values: { ...presetValues("clear_winner"), gapScale: 2 },
    });
    expect(wide.gap).toBeGreaterThan(right.gap);
  });

  it("flags a judge whose score gaps are below the random variation", () => {
    const values = { ...presetValues("clear_winner"), judgeWrong: 0.5 };
    expect(previewMatrix({ scenario: "clear_winner", ctrMode: "demo", arms, values }).judgeSignalWeak).toBe(true);
    // no noise either: the order really is the listed order, so nothing to flag
    expect(
      previewMatrix({ scenario: "clear_winner", ctrMode: "demo", arms, values: { ...values, noiseScale: 0 } })
        .judgeSignalWeak
    ).toBe(false);
  });

  it("reports the drift swap for the drift scenario only", () => {
    const pv = previewMatrix({
      scenario: "drift",
      ctrMode: "demo",
      arms,
      values: { ...presetValues("drift"), driftAtFrac: 0.3 },
    });
    expect(pv.drift).toEqual({ best: pv.bestOverall, worst: 2, atFrac: 0.3 });
    expect(previewMatrix({ scenario: "clear_winner", ctrMode: "demo", arms, values: presetValues("clear_winner") }).drift).toBeNull();
  });
});

describe("spreadRankCtrs", () => {
  it("keeps every rate in (0, 1) at the gap bounds and beyond", () => {
    for (const g of [0.25, 1, 2, 10]) {
      for (const v of spreadRankCtrs([0.06, 0.045, 0.035], g)) {
        expect(v).toBeGreaterThan(0);
        expect(v).toBeLessThan(1);
      }
    }
  });
  it("is the identity at g = 1 and narrows below it", () => {
    spreadRankCtrs([0.06, 0.045, 0.035], 1).forEach((v, i) => expect(v).toBeCloseTo([0.06, 0.045, 0.035][i], 12));
    const narrow = spreadRankCtrs([0.06, 0.045, 0.035], 0.25);
    expect(narrow[0] - narrow[2]).toBeLessThan(0.06 - 0.035);
  });
});

describe("rebalanceMix", () => {
  const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);

  it("rebalances the others proportionally and keeps the total at 1", () => {
    const out = rebalanceMix([0.4, 0.35, 0.25], 0, 0.6);
    expect(out[0]).toBeCloseTo(0.6, 12);
    expect(out[1] / out[2]).toBeCloseTo(0.35 / 0.25, 12);
    expect(sum(out)).toBeCloseTo(1, 12);
  });

  it("never lets a segment fall below the floor", () => {
    const out = rebalanceMix([0.25, 0.25, 0.4, 0.1], 2, 0.9);
    expect(Math.min(...out)).toBeGreaterThanOrEqual(MIX_FLOOR - 1e-12);
    expect(out[2]).toBeCloseTo(1 - 3 * MIX_FLOOR, 12);
    expect(sum(out)).toBeCloseTo(1, 12);
    const pinned = rebalanceMix([0.6, 0.3, 0.05, 0.05], 0, 0.85);
    expect(pinned[2]).toBeCloseTo(MIX_FLOOR, 12);
    expect(pinned[3]).toBeCloseTo(MIX_FLOOR, 12);
    expect(sum(pinned)).toBeCloseTo(1, 12);
  });

  it("clamps the moved segment to [floor, 1 − floor·(n−1)]", () => {
    expect(rebalanceMix([0.5, 0.5], 0, 0)[0]).toBe(MIX_FLOOR);
    expect(rebalanceMix([0.5, 0.25, 0.25], 1, 1)[1]).toBeCloseTo(0.9, 12);
  });

  it("shows whole percentages that sum to 100", () => {
    expect(displayPercents([1 / 3, 1 / 3, 1 / 3])).toEqual([34, 33, 33]);
    expect(displayPercents([0.555, 0.05, 0.295, 0.1]).reduce((a, b) => a + b, 0)).toBe(100);
  });
});

describe("armScoresFromProof", () => {
  it("mirrors the api's snapshot_arms scores", () => {
    const proof = {
      adCopyEval: {
        original_id: 1,
        headline: "h",
        tone_style: "",
        score: {
          overall_score: 0.8,
          passed: true,
          strengths: [],
          improvements: [],
          verdicts: [
            { dimension: "audience_fit", score: 7, verdict: "pass", rationale: "" },
            { dimension: "clarity", score: 12, verdict: "pass", rationale: "" },
          ],
        },
      },
      visualEval: {
        ad_copy_id: 1,
        concept_name: "c",
        score: {
          overall_score: 0.6,
          passed: false,
          strengths: [],
          improvements: [],
          verdicts: [{ dimension: "stopping_power", score: 9, verdict: "pass", rationale: "" }],
        },
      },
    } as Pick<Proof, "adCopyEval" | "visualEval">;
    const s = armScoresFromProof(proof);
    expect(s).toEqual({ audience_fit: 0.7, clarity: 1, ad_copy_overall: 0.8, stopping_power: 0.9, visual_overall: 0.6 });
    expect(armOverall(s)).toBeCloseTo(0.7, 12);
    expect(armOverall({})).toBe(0.5);
  });
});

describe("previewReading", () => {
  const arms = cases.find((c) => c.name === "segment_winners/three/judge_backwards_gap_wide")!.arms;
  const names = ["Alpha", "Bravo", "Charlie"];
  const label = (s: string) => s.replace(/_/g, " ");

  it("names a segment's preference and when the bandit must overrule the judge", () => {
    const pv = previewMatrix({
      scenario: "segment_winners",
      ctrMode: "demo",
      arms,
      values: { ...presetValues("segment_winners"), judgeWrong: 1, gapScale: 2, noiseScale: 0 },
    });
    const text = previewReading(pv, names, label);
    expect(text).toMatch(/will prefer (Alpha|Bravo|Charlie) by \d+\.\d pts/);
    expect(text).toMatch(/the judge favours Alpha, but (Bravo|Charlie) gets the most clicks overall, so the bandit has to overrule the judge\.$/);
  });

  it("says when one creative wins everywhere and the judge agrees", () => {
    const pv = previewMatrix({ scenario: "clear_winner", ctrMode: "demo", arms, values: presetValues("clear_winner") });
    expect(previewReading(pv, names, label)).toMatch(/^Alpha is the best creative for every segment, .* ahead overall; the judge's favourite, Alpha, is also the best overall\.$/);
  });

  it("formats points", () => {
    expect(formatPts(0.018)).toBe("1.8 pts");
    expect(formatPts(0.0042)).toBe("0.42 pts");
    expect(formatPts(-0.01)).toBe("1.0 pt");
  });
});
