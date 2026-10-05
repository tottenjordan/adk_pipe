import { describe, expect, it } from "vitest";
import presets from "@/lib/scenario-presets.generated.json";
import { presetValues } from "@/lib/scenario-preview";
import type { Shift } from "@/lib/experiments";
import { trafficBody as clientBody } from "@/lib/shifts";
import {
  buildRunView,
  changeKind,
  defaultForget,
  describeShift,
  MAX_SHIFTS,
  mobileSegment,
  moveShift,
  newShift,
  normalizeShift,
  parseRunParam,
  parseShiftResponse,
  presetShift,
  recoverySpans,
  regimeLabels,
  runLabel,
  runShifts,
  selectedRun,
  shiftBounds,
  shiftMarkers,
  shiftPreview,
  shiftsToPayload,
  swapTimes,
  toStat,
  trafficRuns,
  urlForRun,
  validateShifts,
  type ShiftContext,
} from "@/lib/shifts";
import { placeMarkers } from "@/components/charts/line-chart";

/** The three live-experiment creatives (screenshot fixture scores, abridged). */
const CREATIVES = [
  {
    creativeId: "aae3f6b4",
    name: "The Tone Dividend Bailout",
    color: "#0f766e",
    scores: { ad_copy_overall: 0.833, visual_overall: 0.917, audience_fit: 0.7, trend_authenticity: 0.8, stopping_power: 0.8 },
  },
  {
    creativeId: "8c0e9ee8",
    name: "Ergonomic Lumbar Relief",
    color: "#8a5a2b",
    scores: { ad_copy_overall: 0.783, visual_overall: 0.767, audience_fit: 0.6, trend_authenticity: 0.7, stopping_power: 0.8 },
  },
  {
    creativeId: "3fe4b3ec",
    name: "The Great Lot Treaty",
    color: "#5f7a1f",
    scores: { ad_copy_overall: 0.85, visual_overall: 0.867, audience_fit: 1.0, trend_authenticity: 0.7, stopping_power: 0.8 },
  },
];

const ctx = (over: Partial<ShiftContext> = {}): ShiftContext => ({
  scenario: "segment_winners",
  ctrMode: "demo",
  horizon: 40_000,
  creatives: CREATIVES,
  values: presetValues(over.scenario ?? "segment_winners"),
  ...over,
});

const B = presets.shifts.bounds;

describe("bounds come from the presets JSON (bandit.config)", () => {
  it("uses the generated kinds, limits and bounds", () => {
    expect(MAX_SHIFTS).toBe(presets.shifts.maxShifts);
    const demo = shiftBounds("segment_winners", "demo");
    expect(demo.atFrac).toEqual(B.atFrac);
    expect(demo.liftPp).toEqual(B.liftPp);
    expect(demo.ctrMultiplier).toEqual(B.ctrMultiplier);
  });

  it("scales lift / drop by the scenario's realistic click-rate ratio (0.2)", () => {
    const r = shiftBounds("segment_winners", "realistic");
    expect(r.liftPp[0]).toBeCloseTo(B.liftPp[0] * 0.2, 10);
    expect(r.dropPp[1]).toBeCloseTo(B.dropPp[1] * 0.2, 10);
    expect(r.atFrac).toEqual(B.atFrac);
  });
});

describe("validateShifts mirrors bandit.config.validate_shifts", () => {
  const c = ctx();
  const ok: Shift = { kind: "promote", atFrac: 0.4, segment: null, creativeId: "aae3f6b4", liftPp: 0.015 };
  const fields = (list: Shift[], cc = c) => validateShifts(list, cc).map((e) => `${e.index}.${e.field}`);

  it("accepts every kind at its defaults", () => {
    const list = (["promote", "demote", "mix", "shock"] as const).map((k) => newShift(k, c));
    expect(validateShifts(list, c)).toEqual([]);
  });

  it("checks every bound inclusively", () => {
    expect(fields([{ ...ok, atFrac: B.atFrac[0] }, { ...ok, atFrac: B.atFrac[1] }])).toEqual([]);
    expect(fields([{ ...ok, atFrac: B.atFrac[0] - 0.01 }])).toEqual(["0.atFrac"]);
    expect(fields([{ ...ok, atFrac: B.atFrac[1] + 0.01 }])).toEqual(["0.atFrac"]);
    expect(fields([{ ...ok, liftPp: B.liftPp[1] + 0.001 }])).toEqual(["0.liftPp"]);
    expect(fields([{ kind: "demote", atFrac: 0.5, creativeId: "leader", dropPp: B.dropPp[0] - 0.001 }])).toEqual([
      "0.dropPp",
    ]);
    const shock: Shift = { kind: "shock", atFrac: 0.6, untilFrac: 0.7, creativeId: "leader", ctrMultiplier: 0.6 };
    expect(fields([{ ...shock, ctrMultiplier: B.ctrMultiplier[0] - 0.01 }])).toEqual(["0.ctrMultiplier"]);
    expect(fields([{ ...shock, ctrMultiplier: B.ctrMultiplier[1] + 0.01 }])).toEqual(["0.ctrMultiplier"]);
    expect(fields([{ ...shock, untilFrac: 1.01 }])).toEqual(["0.untilFrac"]);
  });

  it("realistic magnitudes use the scaled bounds", () => {
    const r = ctx({ ctrMode: "realistic" });
    expect(fields([ok], r)).toEqual(["0.liftPp"]); // 0.015 > 0.006
    expect(fields([{ ...ok, liftPp: 0.004 }], r)).toEqual([]);
  });

  it("a shock lasts at least the minimum window", () => {
    const shock: Shift = { kind: "shock", atFrac: 0.6, untilFrac: 0.61, creativeId: "aae3f6b4", ctrMultiplier: 0.6 };
    expect(fields([shock])).toEqual(["0.untilFrac"]);
    expect(fields([{ ...shock, untilFrac: 0.6 + presets.shifts.minWindow }])).toEqual([]);
  });

  it("checks creatives, the leader kinds, segments and the mix length", () => {
    expect(fields([{ ...ok, creativeId: "nope" }])).toEqual(["0.creativeId"]);
    expect(fields([{ ...ok, creativeId: "leader" }])).toEqual(["0.creativeId"]); // promote can't target the leader
    for (const kind of presets.shifts.leaderKinds) {
      const s =
        kind === "shock"
          ? ({ kind, atFrac: 0.5, untilFrac: 0.6, creativeId: "leader", ctrMultiplier: 0.6 } as Shift)
          : ({ kind, atFrac: 0.5, creativeId: "leader", dropPp: 0.01 } as Shift);
      expect(fields([s]), kind).toEqual([]);
    }
    expect(fields([{ ...ok, segment: "commuters" }])).toEqual(["0.segment"]);
    expect(fields([{ kind: "mix", atFrac: 0.3, segmentMix: [0.5, 0.5] }])).toEqual(["0.segmentMix"]);
    expect(fields([{ kind: "mix", atFrac: 0.3, segmentMix: [0.04, 0.32, 0.32, 0.32] }])).toEqual(["0.segmentMix"]);
  });

  it("allows at most four shifts", () => {
    const five = Array.from({ length: 5 }, (_, i) => ({ ...ok, atFrac: 0.1 + i * 0.1 }));
    expect(fields(five)).toEqual(["-1.shifts"]);
  });
});

describe("payload mapping", () => {
  it("keeps only each kind's fields, camelCase, rounded", () => {
    const list = [
      { key: "a", kind: "promote", atFrac: 0.40001, segment: "mobile_scrollers", creativeId: "aae3f6b4", liftPp: 0.0150004, dropPp: 9 },
      { key: "b", kind: "mix", atFrac: 0.3, segmentMix: [0.6, 0.13333333, 0.13333333, 0.13333334], creativeId: "x" },
      { key: "c", kind: "shock", atFrac: 0.6, untilFrac: 0.75, creativeId: "leader", ctrMultiplier: 0.6 },
      { key: "d", kind: "demote", atFrac: 0.5, creativeId: "leader", dropPp: 0.02 },
    ] as unknown as Shift[];
    expect(shiftsToPayload(list)).toEqual([
      { kind: "promote", atFrac: 0.4, segment: "mobile_scrollers", creativeId: "aae3f6b4", liftPp: 0.015 },
      { kind: "mix", atFrac: 0.3, segmentMix: [0.6, 0.1333, 0.1333, 0.1333] },
      { kind: "shock", atFrac: 0.6, untilFrac: 0.75, segment: null, creativeId: "leader", ctrMultiplier: 0.6 },
      { kind: "demote", atFrac: 0.5, segment: null, creativeId: "leader", dropPp: 0.02 },
    ]);
  });

  it("the traffic body is unchanged without shifts and adds shifts + forget with them", () => {
    expect(clientBody(20, 40_000)).toEqual({ episodes: 20, horizon: 40_000 });
    expect(clientBody(5)).toEqual({ episodes: 5 });
    const s: Shift = { kind: "demote", atFrac: 0.5, creativeId: "leader", dropPp: 0.02 };
    expect(clientBody(20, 40_000, [s])).toEqual({
      episodes: 20,
      horizon: 40_000,
      shifts: [{ kind: "demote", atFrac: 0.5, segment: null, creativeId: "leader", dropPp: 0.02 }],
      forget: true,
    });
    expect(clientBody(20, 40_000, [s], false).forget).toBe(false);
    expect(defaultForget(0)).toBe(false);
    expect(defaultForget(1)).toBe(true);
  });
});

describe("editor operations", () => {
  const c = ctx();

  it("places new events clear of existing ones", () => {
    const a = newShift("demote", c);
    const b = newShift("demote", c, [a]);
    expect(Math.abs(a.atFrac - b.atFrac)).toBeGreaterThan(0.02);
  });

  it("moving a shock keeps its length and stays in bounds", () => {
    const s = newShift("shock", c);
    const len = (s.untilFrac as number) - s.atFrac;
    const moved = moveShift(s, 0.3);
    expect(moved.atFrac).toBe(0.3);
    expect((moved.untilFrac as number) - moved.atFrac).toBeCloseTo(len, 6);
    const end = moveShift(s, 0.95);
    expect(end.untilFrac).toBeLessThanOrEqual(1);
    expect((end.untilFrac as number) - end.atFrac).toBeGreaterThanOrEqual(presets.shifts.minWindow - 1e-9);
  });

  it("swapping times exchanges when two events happen", () => {
    const a = { ...newShift("demote", c), atFrac: 0.5 };
    const b = { ...newShift("promote", c), atFrac: 0.3 };
    const [a2, b2] = swapTimes([a, b], 0, 1);
    expect([a2.atFrac, b2.atFrac]).toEqual([0.3, 0.5]);
    expect(a2.kind).toBe("demote");
  });

  it("changing kind keeps time and drops the leader for promote", () => {
    const d = { ...newShift("demote", c), atFrac: 0.45 };
    const p = changeKind(d, "promote", c);
    expect(p.atFrac).toBe(0.45);
    expect(p.creativeId).not.toBe("leader");
    expect(validateShifts([p], c)).toEqual([]);
  });
});

describe("presets have a visible effect in the preview", () => {
  it("every preset is valid and changes the expected click rates", () => {
    for (const scenario of ["segment_winners", "clear_winner", "drift"] as const) {
      const c = ctx({ scenario, values: presetValues(scenario) });
      for (const id of ["demote-leader-halfway", "mobile-surge", "leader-fatigue"] as const) {
        const s = presetShift(id, c);
        expect(validateShifts([s], c), `${scenario} ${id}`).toEqual([]);
        const pv = shiftPreview(c, [s])!;
        expect(pv.regimes.length).toBeGreaterThanOrEqual(2);
        const [before, after] = pv.regimes;
        const moved =
          after.ctr.some((row, a) => row.some((v, si) => Math.abs(v - before.ctr[a][si]) > 0.002)) ||
          after.segmentWeights.some((w, i) => Math.abs(w - before.segmentWeights[i]) > 0.1);
        expect(moved, `${scenario} ${id}`).toBe(true);
      }
    }
  });

  it("demoting the leader at halfway changes who wins in the segments it led", () => {
    const c = ctx();
    const s = presetShift("demote-leader-halfway", c);
    const pv = shiftPreview(c, [s])!;
    const [before, after] = pv.regimes;
    const leader = before.overall.indexOf(Math.max(...before.overall));
    const led = before.oracle.map((w, i) => (w === leader ? i : -1)).filter((i) => i >= 0);
    expect(led.length).toBeGreaterThan(0);
    for (const i of led) expect(after.oracle[i]).not.toBe(leader);
  });

  it("the mobile surge raises the most mobile segment to 60%", () => {
    const c = ctx();
    const s = presetShift("mobile-surge", c);
    const i = mobileSegment("segment_winners");
    expect(presets.scenarios.segment_winners.segments[i].name).toBe("mobile_scrollers");
    expect(s.segmentMix![i]).toBeCloseTo(0.6, 3);
    expect(s.atFrac).toBe(0.3);
  });

  it("ad fatigue is a ×0.6 shock on the leader from 60% to 75%", () => {
    const s = presetShift("leader-fatigue", ctx());
    expect(s).toMatchObject({ kind: "shock", atFrac: 0.6, untilFrac: 0.75, creativeId: "leader", ctrMultiplier: 0.6, segment: null });
  });
});

describe("plain-language sentences", () => {
  const c = ctx();
  it("names who, when, and how much", () => {
    expect(
      describeShift({ kind: "promote", atFrac: 0.4, segment: "mobile_scrollers", creativeId: "3fe4b3ec", liftPp: 0.015 }, c)
    ).toBe("At 40% of the run, mobile scrollers start preferring The Great Lot Treaty (+1.5 pts over the next best).");
    expect(describeShift({ kind: "demote", atFrac: 0.5, segment: null, creativeId: "leader", dropPp: 0.02 }, c, "X")).toBe(
      "At 50% of the run, everyone goes off the leader at that point (X): it falls 2.0 pts below the next best."
    );
    expect(
      describeShift({ kind: "shock", atFrac: 0.6, untilFrac: 0.75, segment: null, creativeId: "aae3f6b4", ctrMultiplier: 0.6 }, c)
    ).toBe("From 60% to 75% of the run, The Tone Dividend Bailout gets 40% fewer clicks, then recovers.");
    expect(describeShift({ kind: "mix", atFrac: 0.3, segmentMix: [0.6, 0.1333, 0.1333, 0.1334] }, c)).toBe(
      "At 30% of the run, mobile scrollers surge to 60% of readers (from 25%)."
    );
  });
});

describe("run records, markers and regimes", () => {
  const resolved = [
    { kind: "shock", at_frac: 0.6, until_frac: 0.75, round: 24000, end_round: 30000, creative_id: "8c0e9ee8", requested_creative_id: "leader", ctr_multiplier: 0.6 },
    { kind: "demote", atFrac: 0.5, round: 20000, endRound: null, segment: "late_night_casual", creativeId: "aae3f6b4", dropPp: 0.02 },
  ];

  it("normalises snake/camel records and sorts by round", () => {
    const list = runShifts({ shifts: resolved, horizon: 40_000 });
    expect(list.map((s) => [s.kind, s.round, s.endRound, s.creativeId, s.requestedCreativeId])).toEqual([
      ["demote", 20000, null, "aae3f6b4", "aae3f6b4"],
      ["shock", 24000, 30000, "8c0e9ee8", "leader"],
    ]);
    expect(normalizeShift({ kind: "promote", atFrac: 0.4 }, 40_000)?.round).toBe(16000);
    expect(normalizeShift({ kind: "nope", atFrac: 0.4 }, 40_000)).toBeNull();
    expect(normalizeShift(null, 40_000)).toBeNull();
  });

  it("places a marker at each shift round and at a shock's end", () => {
    const list = runShifts({ shifts: resolved, horizon: 40_000 });
    expect(shiftMarkers(list, 40_000)).toEqual([
      { x: 20000, label: "Shift 1" },
      { x: 24000, label: "Shift 2" },
      { x: 30000, label: "Shift 2 ends" },
    ]);
    // A shock that runs to the end of the run has no end marker.
    expect(shiftMarkers([{ round: 100, endRound: 1000, kind: "shock" }], 1000)).toEqual([{ x: 100, label: "Shift 1" }]);
  });

  it("labels regimes by the shift they follow", () => {
    const list = runShifts({ shifts: resolved, horizon: 40_000 });
    const regimes = [{ start: 0 }, { start: 20000 }, { start: 24000 }, { start: 30000 }];
    expect(regimeLabels(regimes, list)).toEqual(["Before", "After shift 1", "During shift 2", "After shift 2 ends"]);
  });

  it("staggers marker labels that would touch", () => {
    const placed = placeMarkers([{ px: 100 }, { px: 120 }, { px: 200 }, { px: 210 }], 400);
    expect(placed.map((m) => m.row)).toEqual([0, 1, 0, 1]);
  });

  it("builds no run view for a run without shifts (older APIs render as before)", () => {
    expect(buildRunView(null, null, null)).toBeNull();
    expect(buildRunView({ shifts: [], horizon: 40_000, forget: false }, { horizon: 40_000 }, null)).toBeNull();
    const v = buildRunView({ shifts: resolved, horizon: 40_000, forget: true }, { horizon: 40_000, regimes: [] }, null)!;
    expect(v.markers).toHaveLength(3);
    expect(v.forget).toBe(true);
  });
});

describe("shift response parsing", () => {
  it("reads the list-of-shifts layout with intervals", () => {
    const raw = [
      {
        round: 20000,
        kind: "demote",
        policies: {
          linear_ts: {
            pctOptimalBefore: { mean: 0.57, lo: 0.5, hi: 0.64, n: 10 },
            pct_optimal_after: 0.44,
            recoveryRounds: { mean: 2634, ci: 1896 },
            recoveredEpisodes: 10,
            episodes: 10,
          },
        },
      },
    ];
    const [r] = parseShiftResponse(raw);
    expect(r.round).toBe(20000);
    expect(r.policies.linear_ts.pctOptimalBefore).toEqual({ mean: 0.57, lo: 0.5, hi: 0.64, n: 10 });
    expect(r.policies.linear_ts.pctOptimalAfter?.mean).toBe(0.44);
    expect(r.policies.linear_ts.recoveryRounds?.lo).toBeCloseTo(738, 0);
  });

  it("reads the CLI's policy → per-shift layout (snake_case)", () => {
    const raw = {
      linear_ts: [{ round: 24000, episodes: 5, pct_optimal_before: 0.49, pct_optimal_after: 0.44, recovery_rounds: null, recovered_episodes: 0 }],
      ucb1: [{ round: 24000, episodes: 5, pct_optimal_before: 0.33, pct_optimal_after: 0.41, recovery_rounds: 1057, recovered_episodes: 5 }],
    };
    const [r] = parseShiftResponse(raw);
    expect(Object.keys(r.policies)).toEqual(["linear_ts", "ucb1"]);
    expect(r.policies.linear_ts.recoveryRounds).toBeNull();
    expect(r.policies.ucb1.recoveryRounds?.mean).toBe(1057);
  });

  it("tolerates garbage", () => {
    expect(parseShiftResponse(undefined)).toEqual([]);
    expect(parseShiftResponse("x")).toEqual([]);
    expect(parseShiftResponse([null, { round: "a" }])).toEqual([]);
    expect(toStat("x")).toBeNull();
  });

  it("turns recovery into shaded spans, to the end when never recovered", () => {
    const results = parseShiftResponse({
      linear_ts: [
        { round: 20000, pct_optimal_before: 0.5, pct_optimal_after: 0.3, recovery_rounds: 2600 },
        { round: 30000, pct_optimal_before: 0.5, pct_optimal_after: 0.3, recovery_rounds: null },
      ],
    });
    expect(recoverySpans(results, 40_000)).toEqual([
      { x0: 20000, x1: 22600, label: "Recovery" },
      { x0: 30000, x1: 40000, label: "Not recovered" },
    ]);
  });
});

describe("run selector URL state", () => {
  it("parses ?run= as a positive integer, else latest", () => {
    expect(parseRunParam("2")).toBe(2);
    for (const bad of [null, undefined, "", "0", "-1", "1.5", "two", "1234567"]) expect(parseRunParam(bad)).toBeNull();
  });

  it("sets and clears run, keeping other params", () => {
    expect(urlForRun("https://x.test/experiments/abc?view=analysis#c", 2)).toBe("/experiments/abc?view=analysis&run=2#c");
    expect(urlForRun("https://x.test/experiments/abc?run=2&view=analysis", null)).toBe("/experiments/abc?view=analysis");
  });

  const runs = trafficRuns([
    { run: 2, startedAt: null, episodes: 10, horizon: 40000, shifts: [{}, {}], forget: true, status: "done" },
    { run: 1, startedAt: null, episodes: 5, horizon: 40000, shifts: [], forget: false, status: "done" },
    { run: 0, startedAt: null, episodes: 5, horizon: 40000, shifts: [], forget: false, status: "done" },
  ]);

  it("selects the requested run, else the latest", () => {
    expect(runs.map((r) => r.run)).toEqual([1, 2]);
    expect(selectedRun(runs, 1)?.run).toBe(1);
    expect(selectedRun(runs, null)?.run).toBe(2);
    expect(selectedRun(runs, 9)?.run).toBe(2);
    expect(selectedRun([], 1)).toBeNull();
    expect(trafficRuns(undefined)).toEqual([]);
  });

  it("labels a run with its shifts and forgetting", () => {
    expect(runLabel(runs[1], 3)).toBe("Run 2 of 3 · 2 shifts · forgetting on");
    expect(runLabel(runs[0], 2)).toBe("Run 1 of 2 · no shifts");
  });
});
