import { describe, expect, it } from "vitest";
import { deployBlockedReason, selectionToPayload } from "@/lib/experiments";
import { overridesFromValues, presetValues, rebalanceMix, valuesFromOverrides } from "@/lib/scenario-preview";

const base = {
  appName: "creative_agent",
  sessionId: "s1",
  scenario: "segment_winners" as const,
  ctrMode: "demo" as const,
  rewardMode: "click" as const,
  ttlMinutes: 120,
};

describe("selectionToPayload", () => {
  it("maps the panel choices onto the POST /experiments body", () => {
    expect(selectionToPayload({ ...base, selected: new Set([3, 0, 1]) })).toEqual({
      userId: "me",
      appName: "creative_agent",
      sessionId: "s1",
      creativeIndices: [0, 1, 3],
      scenario: "segment_winners",
      ctrMode: "demo",
      rewardMode: "click",
      ttlMinutes: 120,
    });
  });
  it("de-duplicates and drops invalid indices", () => {
    expect(selectionToPayload({ ...base, selected: [2, 2, -1, 1.5, 0] }).creativeIndices).toEqual([0, 2]);
  });
  it("passes an empty selection through (the button guards it)", () => {
    expect(selectionToPayload({ ...base, selected: [] }).creativeIndices).toEqual([]);
  });
});

describe("selectionToPayload scenarioOverrides (contracts §9)", () => {
  it("omits scenarioOverrides entirely when every knob is at the preset", () => {
    for (const scenario of ["clear_winner", "segment_winners", "drift"] as const) {
      const body = selectionToPayload({ ...base, scenario, selected: [0, 1], tuning: presetValues(scenario) });
      expect(body).not.toHaveProperty("scenarioOverrides");
    }
    expect(selectionToPayload({ ...base, selected: [0, 1] })).not.toHaveProperty("scenarioOverrides");
  });

  it("sends only the knobs that differ, camelCase", () => {
    const tuning = { ...presetValues("segment_winners"), judgeWrong: 1, gapScale: 1.5 };
    expect(selectionToPayload({ ...base, selected: [0, 1], tuning }).scenarioOverrides).toEqual({
      judgeWrong: 1,
      gapScale: 1.5,
    });
  });

  it("sends the whole mix (rounded, still summing to 1) when any segment moved", () => {
    const tuning = presetValues("segment_winners");
    tuning.segmentMix = rebalanceMix(tuning.segmentMix, 0, 0.55);
    const ov = selectionToPayload({ ...base, selected: [0, 1], tuning }).scenarioOverrides;
    expect(ov?.segmentMix).toEqual([0.55, 0.15, 0.15, 0.15]);
    expect(Object.keys(ov ?? {})).toEqual(["segmentMix"]);
  });

  it("only sends driftAtFrac for the drift scenario", () => {
    const moved = { ...presetValues("clear_winner"), driftAtFrac: 0.3 };
    expect(selectionToPayload({ ...base, scenario: "clear_winner", selected: [0, 1], tuning: moved })).not.toHaveProperty(
      "scenarioOverrides"
    );
    const drift = { ...presetValues("drift"), driftAtFrac: 0.3 };
    expect(
      selectionToPayload({ ...base, scenario: "drift", selected: [0, 1], tuning: drift }).scenarioOverrides
    ).toEqual({ driftAtFrac: 0.3 });
  });

  it("round-trips through valuesFromOverrides", () => {
    const tuning = { ...presetValues("drift"), noiseScale: 0, driftAtFrac: 0.7 };
    const ov = overridesFromValues("drift", tuning);
    expect(valuesFromOverrides("drift", ov)).toEqual(tuning);
    expect(valuesFromOverrides("drift", null)).toEqual(presetValues("drift"));
  });
});

describe("deployBlockedReason", () => {
  it("needs at least two creatives", () => {
    expect(deployBlockedReason(0, false)).toMatch(/at least two/);
    expect(deployBlockedReason(1, false)).toMatch(/at least two/);
    expect(deployBlockedReason(2, false)).toBeNull();
    expect(deployBlockedReason(4, false)).toBeNull();
    expect(deployBlockedReason(5, false)).toMatch(/at most four/);
  });
  it("blocks stopped-early runs", () => {
    expect(deployBlockedReason(3, true)).toMatch(/stopped early/);
  });
});
