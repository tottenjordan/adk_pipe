import { describe, expect, it } from "vitest";
import { deployBlockedReason, selectionToPayload } from "@/lib/experiments";

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
