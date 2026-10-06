import { afterEach, describe, expect, it, vi } from "vitest";
import {
  continuousLimitError,
  curveSeries,
  isContinuousRun,
  MAX_CONTINUOUS_ROUNDS,
  parseContinuousSummary,
  startTraffic,
  type Arm,
  type ExperimentMetrics,
  type TrafficRun,
} from "@/lib/experiments";
import {
  buildInsights,
  continuousHeadline,
  continuousVerdict,
  keptSegments,
  type ExperimentInsights,
} from "@/lib/experiment-insights";
import { buildRunView, runLabel, runRounds, segmentMarkers, trafficBody } from "@/lib/shifts";
import { placeMarkers } from "@/components/charts/line-chart";
import { CONTROL_HELP } from "@/lib/experiment-help";
import { jsonResponse } from "./helpers";
import contSummaryJson from "../../scripts/screenshot-fixtures/continuous-experiment.json";
import contMetricsJson from "../../scripts/screenshot-fixtures/continuous-experiment-metrics.json";
import contCreativesJson from "../../scripts/screenshot-fixtures/continuous-experiment-creatives.json";

afterEach(() => vi.unstubAllGlobals());

const ID = "exp-cont";

function run(over: Partial<TrafficRun> = {}): TrafficRun {
  return {
    run: 2,
    startedAt: null,
    episodes: 20,
    horizon: 40_000,
    shifts: [],
    forget: false,
    status: "finished",
    ...over,
  };
}

/** A continuous payload: stitched global checkpoints, lo = hi = mean (no bands). */
const flat = (mean: number[]) => ({ mean, lo: [...mean], hi: [...mean] });
function contMetrics(summary?: unknown): ExperimentMetrics {
  return {
    experimentId: ID,
    episodes: 20,
    // PR B's continuous shape: horizon = rounds covered, plus the segment length and starts.
    horizon: 800_000,
    learning: "continuous",
    segmentHorizon: 40_000,
    segmentStarts: Array.from({ length: 20 }, (_, i) => i * 40_000),
    checkpoints: [40_000, 400_000, 800_000],
    policies: ["linear_ts", "ucb1", "uniform", "oracle"],
    curves: {
      linear_ts: { cumAvgReward: flat([0.04, 0.05, 0.051]), cumRegret: flat([300, 900, 1500]), pctOptimal: flat([0.5, 0.8, 0.85]) },
      ucb1: { cumAvgReward: flat([0.04, 0.045, 0.046]), cumRegret: flat([300, 2900, 5600]), pctOptimal: flat([0.4, 0.5, 0.5]) },
      uniform: { cumAvgReward: flat([0.035, 0.035, 0.035]), cumRegret: flat([400, 3900, 7800]), pctOptimal: flat([0.33, 0.33, 0.33]) },
      oracle: { cumAvgReward: flat([0.055, 0.055, 0.055]), cumRegret: flat([0, 0, 0]), pctOptimal: flat([1, 1, 1]) },
    },
    totals: {
      linear_ts: { mean: 2040, std: 60 },
      ucb1: { mean: 1840, std: 40 },
      uniform: { mean: 1400, std: 30 },
      oracle: { mean: 2200, std: 30 },
    },
    armShare: { a1: [0.4, 0.7, 0.8], b2: [0.6, 0.3, 0.2] },
    perSegment: {},
    arms: [],
    run: 2,
    ...(summary === undefined ? {} : { continuousSummary: summary }),
  };
}

function summaryWith(pd: Partial<Record<string, unknown>>, segments = 20, warmupSegments = 10) {
  return {
    segments,
    warmupSegments,
    pairedDiff: { policy: "ucb1", perSegment: [], mean: 210.4, lo: 180.2, hi: 240.6, lag1: 0.05, status: "ok", ...pd },
  };
}

const ARMS: Arm[] = [
  { creativeId: "a1", index: 0, label: "Alpha", conceptName: "Alpha", imageUri: null, scores: {}, overallScore: null },
  { creativeId: "b2", index: 1, label: "Bravo", conceptName: "Bravo", imageUri: null, scores: {}, overallScore: null },
];

const insightsFor = (summary?: unknown, extra: Record<string, unknown> = {}): ExperimentInsights =>
  buildInsights({
    metrics: contMetrics(summary),
    arms: ARMS,
    rewardMode: "click",
    ctrMode: "demo",
    scenario: "segment_winners",
    continuous: { segments: 20, totalRounds: 800_000 },
    ...extra,
  });

// ── C1: control + payload ─────────────────────────────────────────────────────

describe("traffic payload (contracts §5/§11)", () => {
  it("sends learning only for a continuous run", () => {
    expect(trafficBody(20, 40_000)).toEqual({ episodes: 20, horizon: 40_000 });
    expect(trafficBody(20, 40_000, [], undefined, "per_episode")).toEqual({ episodes: 20, horizon: 40_000 });
    expect(trafficBody(20, 40_000, [], undefined, "continuous")).toEqual({
      episodes: 20,
      horizon: 40_000,
      learning: "continuous",
    });
    const withShift = trafficBody(
      5,
      40_000,
      [{ kind: "promote", atFrac: 0.5, segment: null, creativeId: "a1", liftPp: 0.01 }],
      undefined,
      "continuous"
    );
    expect(withShift.learning).toBe("continuous");
    expect(withShift.forget).toBe(true);
  });

  it("startTraffic passes learning through to the body", async () => {
    const fetchMock = vi.fn(async (_u: string, _i?: RequestInit) => jsonResponse({ status: "running_traffic" }));
    vi.stubGlobal("fetch", fetchMock);
    await startTraffic(ID, 20, 40_000, { learning: "continuous" });
    expect(JSON.parse(fetchMock.mock.calls[0][1]?.body as string)).toEqual({
      episodes: 20,
      horizon: 40_000,
      learning: "continuous",
    });
    await startTraffic(ID, 20, 40_000, { learning: "per_episode" });
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({ episodes: 20, horizon: 40_000 });
  });

  it("explains an invalid_learning 400", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: { reason: "invalid_learning", field: "episodes", message: "too long" } }, 400))
    );
    await expect(startTraffic(ID, 50, 400_000, { learning: "continuous" })).rejects.toThrow(/learning mode \(episodes\): too long/);
  });
});

describe("total-rounds cap", () => {
  it("allows up to 2,000,000 rounds in one stream", () => {
    expect(MAX_CONTINUOUS_ROUNDS).toBe(2_000_000);
    expect(continuousLimitError(20, 40_000)).toBeNull(); // 800,000
    expect(continuousLimitError(50, 40_000)).toBeNull(); // exactly 2,000,000
    expect(continuousLimitError(5, 400_000)).toBeNull();
  });
  it("refuses longer streams, naming the total", () => {
    expect(continuousLimitError(20, 400_000)).toMatch(/2,000,000 rounds; 20 segments of 400,000 would be 8,000,000/);
    expect(continuousLimitError(50, 200_000)).not.toBeNull();
  });
  it("needs whole update batches per segment", () => {
    expect(continuousLimitError(5, 40_050)).toMatch(/multiple of the 100-reader/);
    expect(continuousLimitError(5, 40_050, 50)).toBeNull();
  });
});

describe("run records", () => {
  it("labels continuous runs", () => {
    expect(runLabel(run({ learning: "continuous" }), 2)).toBe("Run 2 of 2 · no shifts · keeps learning");
    expect(runLabel(run({ learning: "per_episode" }), 2)).toBe("Run 2 of 2 · no shifts");
    expect(runLabel(run(), 2)).toBe("Run 2 of 2 · no shifts");
    expect(runLabel(run({ learning: "continuous", forget: true, shifts: [{ kind: "mix", atFrac: 0.5, segmentMix: [1, 1, 1] }] }), 3)).toBe(
      "Run 2 of 3 · 1 shift · forgetting on · keeps learning"
    );
  });
  it("reads legacy runs (no field) as per episode", () => {
    expect(isContinuousRun(run())).toBe(false);
    expect(isContinuousRun(run({ learning: "per_episode" }))).toBe(false);
    expect(isContinuousRun(run({ learning: "continuous" }))).toBe(true);
    expect(isContinuousRun(null)).toBe(false);
  });
  it("measures a continuous run on the whole stream", () => {
    expect(runRounds(run())).toBe(40_000);
    expect(runRounds(run({ learning: "continuous" }))).toBe(800_000);
    expect(runRounds(null, 20_000)).toBe(20_000);
    expect(runRounds(null)).toBeNull();
  });
  it("places a continuous run's shifts on the global round", () => {
    const view = buildRunView(
      run({ learning: "continuous", shifts: [{ kind: "promote", atFrac: 0.5, segment: null, creativeId: "a1", liftPp: 0.01 }] }),
      contMetrics(),
      null
    );
    expect(view?.horizon).toBe(800_000);
    expect(view?.shifts[0].round).toBe(400_000);
    expect(view?.markers).toEqual([{ x: 400_000, label: "Shift 1" }]);
  });
  it("has help copy for the learning control that no longer says every episode resets", () => {
    expect(CONTROL_HELP.learning).toMatch(/Reset each episode/);
    expect(CONTROL_HELP.learning).toMatch(/Keep learning/);
    expect(CONTROL_HELP.episodes).toMatch(/segment/);
    expect(CONTROL_HELP.episodes).not.toMatch(/^An episode is one independent run: the endpoint's model is reset/);
  });
});

// ── C2: charts + readings ─────────────────────────────────────────────────────

describe("continuous charts", () => {
  it("draws no bands for a continuous run, on the global rounds", () => {
    const m = contMetrics();
    const s = curveSeries(m, "cumRegret", { bands: true, continuous: true });
    expect(s.every((x) => x.band === undefined)).toBe(true);
    expect(s[0].points.map((p) => p.x)).toEqual([40_000, 400_000, 800_000]);
    // Per-episode payloads keep their bands.
    const banded = { ...m, curves: { ...m.curves, linear_ts: { ...m.curves.linear_ts, cumRegret: { mean: [1, 2, 3], lo: [0, 1, 2], hi: [2, 3, 4] } } } };
    expect(curveSeries(banded, "cumRegret", { bands: true })[0].band).toHaveLength(3);
  });

  it("places subtle segment-boundary ticks at each segment start", () => {
    const ticks = segmentMarkers(run({ episodes: 5, learning: "continuous" }));
    expect(ticks.map((t) => t.x)).toEqual([0, 40_000, 80_000, 120_000, 160_000]);
    expect(ticks.every((t) => t.subtle)).toBe(true);
    expect(ticks[0].label).toBe("Segment 1 of 5");
    expect(ticks[4].label).toBe("Segment 5 of 5");
  });

  it("prefers the api's segmentStarts (a run still in progress)", () => {
    const ticks = segmentMarkers(run({ episodes: 20, learning: "continuous" }), { starts: [80_000, 0, 40_000] });
    expect(ticks.map((t) => t.x)).toEqual([0, 40_000, 80_000]);
    expect(ticks[2].label).toBe("Segment 3 of 20");
    // Without a run record's rounds, the segment length comes from the metrics.
    expect(segmentMarkers(run({ episodes: 3, horizon: null, learning: "continuous" }), { segmentRounds: 10_000 }).map((t) => t.x)).toEqual([
      0, 10_000, 20_000,
    ]);
  });

  it("has no segment ticks for per-episode or single-segment runs", () => {
    expect(segmentMarkers(run())).toEqual([]);
    expect(segmentMarkers(run({ episodes: 1, learning: "continuous" }))).toEqual([]);
  });

  it("keeps marker label placement working alongside ticks", () => {
    expect(placeMarkers([{ px: 100 }, { px: 120 }], 400).map((m) => m.row)).toEqual([0, 1]);
  });
});

describe("continuousSummary parsing", () => {
  it("reads a valid summary", () => {
    const s = parseContinuousSummary(summaryWith({}));
    expect(s?.pairedDiff.status).toBe("ok");
    expect(s && keptSegments(s)).toBe(10);
  });
  it("is null when absent or malformed", () => {
    expect(parseContinuousSummary(undefined)).toBeNull();
    expect(parseContinuousSummary({ segments: 5 })).toBeNull();
    expect(parseContinuousSummary(summaryWith({ status: "great" }))).toBeNull();
  });
  it("drops the interval unless the checks passed", () => {
    const s = parseContinuousSummary(summaryWith({ status: "autocorrelated" }));
    expect(s?.pairedDiff.lo).toBeNull();
    expect(s?.pairedDiff.hi).toBeNull();
    // "ok" without an interval can't be read as ok.
    expect(parseContinuousSummary(summaryWith({ lo: null }))?.pairedDiff.status).toBe("too_few_segments");
  });
});

describe("continuous readings", () => {
  it("ok and clear of zero: the batch-means headline, ahead", () => {
    const i = insightsFor(summaryWith({}));
    expect(i.verdict).toBe("ahead");
    expect(i.headline).toBe(
      "Over the last 10 segments the endpoint earned 210 more clicks per segment than UCB (± 30, batch means after warm-up)."
    );
    expect(i.support).toMatch(/^20 segments of 40,000 simulated readers in one stream that kept learning/);
    expect(i.support).toMatch(/95% batch-means interval/);
    expect(i.support).toMatch(/after the first 10 \(the warm-up\)/);
  });

  it("ok and below zero: the baseline is ahead", () => {
    const i = insightsFor(summaryWith({ mean: -12.4, lo: -20, hi: -4.8 }));
    expect(i.verdict).toBe("behind");
    expect(i.headline).toBe(
      "Over the last 10 segments UCB earned 12 more clicks per segment than the endpoint (± 7.6, batch means after warm-up)."
    );
    // The "why" line reads the per-segment totals.
    const m = contMetrics(summaryWith({ mean: -12.4, lo: -20, hi: -4.8 }));
    m.totals.ucb1 = { mean: 2100, std: 40 };
    const behind = buildInsights({ metrics: m, arms: ARMS, continuous: { segments: 20, totalRounds: 800_000 } });
    expect(behind.why).toMatch(/per segment than your endpoint/);
  });

  it("ok but straddling zero: too close to call", () => {
    const i = insightsFor(summaryWith({ mean: 3.2, lo: -1.5, hi: 7.9 }));
    expect(i.verdict).toBe("too_early");
    expect(i.headline).toMatch(/about the same clicks per segment \(\+3\.2 ± 4\.7, batch means after warm-up\): too close to call/);
    expect(i.readings.totals).toMatch(/interval includes zero/);
  });

  it("still_trending", () => {
    const i = insightsFor(summaryWith({ status: "still_trending" }));
    expect(i.verdict).toBe("too_early");
    expect(i.headline).toBe(
      "Still learning: the endpoint's per-segment advantage over UCB is still changing, so there is no steady-state estimate yet."
    );
    expect(i.readings.totals).toMatch(/still changing/);
  });

  it("too_few_segments", () => {
    const i = insightsFor(summaryWith({ status: "too_few_segments" }, 6, 3));
    expect(i.headline).toBe(
      "Not enough segments to estimate: only 3 are left after the 3-segment warm-up to compare the endpoint with UCB. Run more segments."
    );
  });

  it("autocorrelated", () => {
    const i = insightsFor(summaryWith({ status: "autocorrelated", lag1: 0.62 }));
    expect(i.headline).toBe(
      "Segments too correlated to estimate: neighbouring segments move together (lag-1 correlation 0.62), so a batch-means interval against UCB would look tighter than it is."
    );
  });

  it("falls back honestly when the api sends no summary", () => {
    const i = insightsFor(undefined);
    // The flat curves (lo = hi) must not be read as a settled win.
    expect(i.verdict).toBe("too_early");
    expect(i.headline).toMatch(/^The endpoint kept learning across 20 segments; there is no batch-means estimate yet/);
    expect(continuousHeadline(null, 1, null)).toMatch(/1 segment;/);
  });

  it("never uses the per-episode wording", () => {
    for (const s of [summaryWith({}), summaryWith({ status: "still_trending" }), undefined]) {
      const i = insightsFor(s);
      const text = [i.headline, i.detail, i.support, ...Object.values(i.readings)].filter(Boolean).join("\n");
      expect(text).not.toMatch(/Too early to call/);
      expect(text).not.toMatch(/per episode/);
      expect(text).not.toMatch(/Run more episodes/);
      expect(text).not.toMatch(/bands still overlap/);
    }
    expect(insightsFor(summaryWith({})).readings.totals).toMatch(/per segment/);
  });

  it("verdict helper ignores the curves entirely", () => {
    expect(continuousVerdict(contMetrics(), null)).toBe("too_early");
    expect(continuousVerdict(null, null)).toBe("empty");
  });

  it("reads a continuous shift response as one stream, not 'too early'", () => {
    const shiftRun = run({
      learning: "continuous",
      shifts: [{ kind: "promote", atFrac: 0.5, segment: null, creativeId: "a1", liftPp: 0.01 }],
    });
    const m: ExperimentMetrics = {
      ...contMetrics(summaryWith({})),
      shiftResponse: {
        linear_ts: [
          {
            round: 400_000,
            continuous: true,
            pctOptimalBefore: { mean: 0.8, lo: null, hi: null },
            pctOptimalAfter: { mean: 0.7, lo: null, hi: null },
            recoveryRounds: { mean: 1200, lo: null, hi: null },
            recoveredEpisodes: 1,
            episodes: 1,
          },
        ],
      },
    };
    const view = buildRunView(shiftRun, m, null);
    const i = buildInsights({
      metrics: m,
      arms: ARMS,
      shifts: view?.shifts,
      continuous: { segments: 20, totalRounds: 800_000 },
    });
    expect(i.shiftCards[0].status).not.toBe("too_early");
    expect(i.shiftCards[0].when).toBe("Round 400,000, 50% of the run");
    expect(i.shiftCards[0].evidence).toMatch(/One continuous stream/);
  });
});

describe("the real continuous fixture (build_continuous_fixture.py)", () => {
  const m = contMetricsJson as unknown as ExperimentMetrics;
  const exp = contSummaryJson as unknown as { arms: Arm[]; trafficRuns: TrafficRun[] };
  it("is a continuous run with stitched, band-free curves", () => {
    const r = exp.trafficRuns[exp.trafficRuns.length - 1];
    expect(isContinuousRun(r)).toBe(true);
    const lin = m.curves.linear_ts.cumRegret;
    expect(lin.lo).toEqual(lin.mean);
    // Cumulative regret carries across segment boundaries: never drops.
    expect(lin.mean.every((v, i) => i === 0 || v >= lin.mean[i - 1] - 1e-6)).toBe(true);
    expect(m.checkpoints[m.checkpoints.length - 1]).toBe(r.episodes * (r.horizon as number));
    expect(parseContinuousSummary(m.continuousSummary)).not.toBeNull();
  });
  it("builds a headline from its summary", () => {
    const r = exp.trafficRuns[exp.trafficRuns.length - 1];
    const i = buildInsights({
      metrics: m,
      series: contCreativesJson as never,
      arms: exp.arms,
      continuous: { segments: r.episodes, totalRounds: runRounds(r) },
    });
    expect(i.verdict).not.toBe("empty");
    expect(i.headline.length).toBeGreaterThan(40);
    expect(i.headline).not.toMatch(/NaN|undefined/);
  });
});
