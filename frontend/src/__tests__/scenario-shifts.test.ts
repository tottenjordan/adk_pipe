import { describe, expect, it } from "vitest";
import golden from "./fixtures/scenario-shifts-golden.json";
import type { Scenario, ScenarioOverrides, Shift } from "@/lib/experiments";
import { applyShifts, previewMatrix, shiftRound, valuesFromOverrides } from "@/lib/scenario-preview";

/** ±0.2 percentage points, the preview's parity bound. */
const PARITY_TOL = 0.002;
/** What the exact-expectation port achieves (bandit's α Monte Carlo is the only gap). */
const ACHIEVED_TOL = 0.00002;

type GoldenTarget = { segment: string; ctrBefore: number; ctrAfter: number; bestOtherCtr?: number; logitOffset?: number };
type GoldenCase = {
  name: string;
  scenario: Scenario;
  ctrMode: "demo" | "realistic";
  horizon: number;
  arms: Record<string, number>[];
  overrides: ScenarioOverrides;
  shifts: Shift[];
  expected: {
    segments: string[];
    alpha: number;
    resolved: {
      index: number;
      kind: string;
      round: number;
      endRound: number | null;
      creativeIndex: number | null;
      segment: string | null;
      segmentWeights: number[] | null;
      targets: GoldenTarget[];
    }[];
    regimes: {
      start: number;
      end: number;
      active: number[];
      segmentWeights: number[];
      ctr: number[][];
      oracle: number[];
      overall: number[];
    }[];
  };
};
const cases = (golden as unknown as { cases: GoldenCase[] }).cases;

const run = (c: GoldenCase) => {
  const pv = previewMatrix({
    scenario: c.scenario,
    ctrMode: c.ctrMode,
    arms: c.arms,
    values: valuesFromOverrides(c.scenario, c.overrides),
  });
  return { pv, sp: applyShifts(pv, c.shifts, { horizon: c.horizon, armIds: c.arms.map((_, i) => `arm-${i}`) }) };
};

const close = (got: number, want: number, what: string) => {
  expect(Math.abs(got - want), what).toBeLessThanOrEqual(PARITY_TOL);
  expect(Math.abs(got - want), what).toBeLessThanOrEqual(ACHIEVED_TOL);
};

describe("applyShifts golden parity with bandit (scenario-shifts-golden.json)", () => {
  it("covers every shift kind, a leader shock and a realistic case", () => {
    const kinds = new Set(cases.flatMap((c) => c.shifts.map((s) => s.kind)));
    expect(kinds).toEqual(new Set(["promote", "demote", "mix", "shock"]));
    expect(cases.some((c) => c.shifts.some((s) => s.kind === "shock" && s.creativeId === "leader"))).toBe(true);
    expect(cases.some((c) => c.ctrMode === "realistic")).toBe(true);
  });

  it.each(cases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const { pv, sp } = run(c);
    expect(pv.segments).toEqual(c.expected.segments);
    expect(Math.abs(pv.alpha - c.expected.alpha)).toBeLessThan(1e-3);

    expect(sp.resolved.map((r) => [r.index, r.kind, r.round, r.endRound, r.creativeIndex, r.segment])).toEqual(
      c.expected.resolved.map((r) => [r.index, r.kind, r.round, r.endRound, r.creativeIndex, r.segment])
    );
    sp.resolved.forEach((r, j) => {
      const want = c.expected.resolved[j];
      if (want.segmentWeights) {
        r.segmentWeights!.forEach((w, s) => close(w, want.segmentWeights![s], `${c.name} mix ${s}`));
      }
      expect(r.targets.map((t) => t.segment)).toEqual(want.targets.map((t) => t.segment));
      r.targets.forEach((t, k) => {
        const wt = want.targets[k];
        close(t.ctrBefore, wt.ctrBefore, `${c.name} #${j} ctrBefore`);
        close(t.ctrAfter, wt.ctrAfter, `${c.name} #${j} ctrAfter`);
        if (wt.bestOtherCtr !== undefined) close(t.bestOtherCtr!, wt.bestOtherCtr, `${c.name} #${j} bestOther`);
      });
    });

    expect(sp.regimes.map((g) => [g.start, g.end, g.active, g.oracle])).toEqual(
      c.expected.regimes.map((g) => [g.start, g.end, g.active, g.oracle])
    );
    sp.regimes.forEach((g, j) => {
      const want = c.expected.regimes[j];
      g.segmentWeights.forEach((w, s) => close(w, want.segmentWeights[s], `${c.name} regime ${j} weight ${s}`));
      g.ctr.forEach((row, a) => row.forEach((v, s) => close(v, want.ctr[a][s], `${c.name} regime ${j} ${a}×${s}`)));
      g.overall.forEach((v, a) => close(v, want.overall[a], `${c.name} regime ${j} overall ${a}`));
    });
  });
});

describe("shiftRound", () => {
  it("rounds half to even like Python", () => {
    expect(shiftRound(0.5, 40_000)).toBe(20_000);
    expect(shiftRound(0.00125, 1000)).toBe(1); // 1.25
    expect(shiftRound(0.0025, 1000)).toBe(2); // 2.5 → 2
    expect(shiftRound(0.0035, 1000)).toBe(4); // 3.5 → 4
  });
});
