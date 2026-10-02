import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ActiveExperimentError,
  armColor,
  armImageUrl,
  armShareSeries,
  armStatRows,
  canStop,
  createExperiment,
  curveSeries,
  defaultHorizon,
  ExperimentApiError,
  getExperiment,
  getExperimentMetrics,
  hasMetrics,
  listExperiments,
  orderPolicies,
  policyLabel,
  pollExperiment,
  scenarioLabel,
  segmentRows,
  startTraffic,
  statusInfo,
  stopExperiment,
  totalBars,
  ttlText,
  type Arm,
  type ExperimentMetrics,
  type ExperimentSummary,
} from "@/lib/experiments";

const BASE = "/api/adk";
const ID = "exp-3f9a2c1d";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function summary(status: ExperimentSummary["status"]): ExperimentSummary {
  return {
    experimentId: ID,
    userId: "u",
    sessionId: "s",
    appName: "creative_agent",
    createdAt: "2026-10-02T10:00:00Z",
    updatedAt: "2026-10-02T10:00:00Z",
    status,
    scenario: "segment_winners",
    ctrMode: "demo",
    rewardMode: "click",
    ttlExpiresAt: null,
    arms: [],
    endpointId: null,
    trafficExecution: null,
    progress: null,
    error: null,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("REST wrappers", () => {
  it("createExperiment posts the body and returns the new id", async () => {
    const fetchMock = vi.fn(async (_u: string, _i?: RequestInit) =>
      jsonResponse({ experimentId: ID, status: "deploying" }, 201)
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = {
      userId: "me",
      appName: "creative_agent",
      sessionId: "s1",
      creativeIndices: [0, 2],
      scenario: "clear_winner" as const,
      ctrMode: "demo" as const,
      rewardMode: "click" as const,
      ttlMinutes: 120,
    };
    await expect(createExperiment(body)).resolves.toEqual({ experimentId: ID, status: "deploying" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE}/experiments`);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual(body);
  });

  it("createExperiment throws ActiveExperimentError on 409 active_experiment", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: { reason: "active_experiment" } }, 409))
    );
    const err = await createExperiment({} as never).catch((e) => e);
    expect(err).toBeInstanceOf(ActiveExperimentError);
  });

  it("createExperiment surfaces other errors with the status", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "fewer than 2 arms" }, 400)));
    const err = await createExperiment({} as never).catch((e) => e);
    expect(err).toBeInstanceOf(ExperimentApiError);
    expect(err.status).toBe(400);
    expect(err.message).toMatch(/fewer than 2 arms/);
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: { reason: "other" } }, 409)));
    const err409 = await createExperiment({} as never).catch((e) => e);
    expect(err409).not.toBeInstanceOf(ActiveExperimentError);
    expect(err409.status).toBe(409);
  });

  it("list/get/metrics hit the user-scoped routes with the 'me' placeholder", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("/metrics")) return jsonResponse({ experimentId: ID, episodes: 0 });
      if (url.endsWith(ID)) return jsonResponse(summary("ready"));
      return jsonResponse({ experiments: [summary("ready")] });
    });
    vi.stubGlobal("fetch", fetchMock);
    expect(await listExperiments()).toHaveLength(1);
    expect((await getExperiment(ID)).status).toBe("ready");
    expect((await getExperimentMetrics(ID)).episodes).toBe(0);
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
      `${BASE}/experiments/me`,
      `${BASE}/experiments/me/${ID}`,
      `${BASE}/experiments/me/${ID}/metrics`,
    ]);
  });

  it("listExperiments tolerates a missing list", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({})));
    expect(await listExperiments()).toEqual([]);
  });

  it("startTraffic posts episodes and horizon; 409 explains readiness", async () => {
    const fetchMock = vi.fn(async (_u: string, _i?: RequestInit) =>
      jsonResponse({ status: "running_traffic", execution: "x" })
    );
    vi.stubGlobal("fetch", fetchMock);
    await startTraffic(ID, 20, 40000);
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/experiments/me/${ID}/traffic`);
    expect(JSON.parse(fetchMock.mock.calls[0][1]?.body as string)).toEqual({ episodes: 20, horizon: 40000 });
    await startTraffic(ID, 5);
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({ episodes: 5 });
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "not ready" }, 409)));
    await expect(startTraffic(ID, 5)).rejects.toThrow(/ready/);
  });

  it("stopExperiment posts to the stop route", async () => {
    const fetchMock = vi.fn(async (_u: string, _i?: RequestInit) => jsonResponse({ status: "stopping" }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(stopExperiment(ID)).resolves.toEqual({ status: "stopping" });
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/experiments/me/${ID}/stop`);
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
  });
});

describe("pollExperiment", () => {
  it("yields each summary and stops once the status settles", async () => {
    const seq = ["deploying", "deploying", "ready", "running_traffic"] as const;
    let i = 0;
    const fetchMock = vi.fn(async () => jsonResponse(summary(seq[i++])));
    vi.stubGlobal("fetch", fetchMock);
    const seen: string[] = [];
    for await (const s of pollExperiment(ID, { intervalMs: 0 })) seen.push(s.status);
    expect(seen).toEqual(["deploying", "deploying", "ready"]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("keeps polling through running_traffic and stopping", async () => {
    const seq = ["running_traffic", "stopping", "stopped"] as const;
    let i = 0;
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(summary(seq[i++]))));
    const seen: string[] = [];
    for await (const s of pollExperiment(ID, { intervalMs: 0 })) seen.push(s.status);
    expect(seen).toEqual(["running_traffic", "stopping", "stopped"]);
  });

  it("throws on an HTTP error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "Not found" }, 404)));
    const gen = pollExperiment(ID, { intervalMs: 0 });
    await expect(gen.next()).rejects.toThrow(/404/);
  });

  it("stops waiting when aborted", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(summary("deploying"))));
    const ctrl = new AbortController();
    const gen = pollExperiment(ID, { intervalMs: 60_000, signal: ctrl.signal });
    await gen.next();
    const pending = gen.next();
    ctrl.abort();
    await expect(pending).rejects.toBeDefined();
  });
});

describe("labels", () => {
  it("maps policies to readable names", () => {
    expect(policyLabel("linear_ts")).toBe("Linear Thompson sampling (endpoint)");
    expect(policyLabel("ucb1")).toBe("UCB");
    expect(policyLabel("epsilon_greedy")).toBe("ε-greedy");
    expect(policyLabel("beta_bernoulli_ts")).toBe("Thompson sampling (no context)");
    expect(policyLabel("uniform")).toBe("Uniform random");
    expect(policyLabel("oracle")).toBe("Oracle");
    expect(policyLabel("mystery")).toBe("mystery");
  });
  it("maps scenarios and statuses", () => {
    expect(scenarioLabel("segment_winners")).toBe("Segment-specific winners");
    expect(statusInfo("running_traffic")).toEqual({ label: "Running traffic", tone: "active" });
    expect(statusInfo("ready").tone).toBe("pass");
    expect(statusInfo("failed").tone).toBe("fail");
    expect(statusInfo("weird_state").label).toBe("Weird state");
  });
  it("knows when an experiment can be stopped", () => {
    expect(canStop("ready")).toBe(true);
    expect(canStop("deploying")).toBe(true);
    expect(canStop("stopping")).toBe(false);
    expect(canStop("expired")).toBe(false);
  });
  it("picks a default horizon by scenario and mode", () => {
    expect(defaultHorizon("clear_winner", "demo")).toBe(20000);
    expect(defaultHorizon("drift", "demo")).toBe(40000);
    expect(defaultHorizon("clear_winner", "realistic")).toBe(200000);
    expect(defaultHorizon("segment_winners", "realistic")).toBe(400000);
  });
  it("builds a proxy URL for gs:// arm images", () => {
    expect(armImageUrl({ imageUri: "gs://b/f/x.png" })).toBe("/api/gcs?bucket=b&path=f%2Fx.png");
    expect(armImageUrl({ imageUri: null })).toBeNull();
  });
});

describe("ttlText", () => {
  const now = Date.parse("2026-10-02T10:00:00Z");
  it("counts down in minutes and hours", () => {
    expect(ttlText("2026-10-02T10:12:30Z", now)).toBe("12 min left");
    expect(ttlText("2026-10-02T11:25:00Z", now)).toBe("1 h 25 min left");
    expect(ttlText("2026-10-02T12:00:00Z", now)).toBe("2 h left");
    expect(ttlText("2026-10-02T10:00:30Z", now)).toBe("Less than a minute left");
  });
  it("handles expiry and missing values", () => {
    expect(ttlText("2026-10-02T09:59:00Z", now)).toBe("Expired");
    expect(ttlText(null, now)).toBe("");
    expect(ttlText("garbage", now)).toBe("");
  });
});

const ARMS: Arm[] = [
  { creativeId: "bbbb2222xx", index: 2, label: "Jackpot", conceptName: "The Jackpot Reveal", imageUri: null, scores: {}, overallScore: 0.69 },
  { creativeId: "aaaa1111xx", index: 0, label: "Golf cart", conceptName: "The Golden Golf Cart Gig", imageUri: null, scores: {}, overallScore: 0.86 },
];

function band(mean: number[]) {
  return { mean, lo: mean.map((v) => v - 0.1), hi: mean.map((v) => v + 0.1) };
}

const METRICS: ExperimentMetrics = {
  experimentId: ID,
  episodes: 20,
  horizon: 1000,
  checkpoints: [10, 100, 1000],
  policies: ["oracle", "uniform", "linear_ts", "ucb1"],
  curves: {
    linear_ts: { cumAvgReward: band([0.03, 0.04, 0.05]), cumRegret: band([1, 5, 9]), pctOptimal: band([0.3, 0.6, 0.9]) },
    ucb1: { cumAvgReward: band([0.03, 0.035, 0.04]), cumRegret: band([1, 7, 20]), pctOptimal: band([0.3, 0.5, 0.6]) },
    uniform: { cumAvgReward: band([0.03, 0.03, 0.03]), cumRegret: band([1, 10, 40]), pctOptimal: band([0.33, 0.33, 0.33]) },
    oracle: { cumAvgReward: band([0.06, 0.06, 0.06]), cumRegret: band([0, 0, 0]), pctOptimal: band([1, 1, 1]) },
  },
  totals: { uniform: { mean: 30, std: 4 }, linear_ts: { mean: 50, std: 5 }, oracle: { mean: 60, std: 3 } },
  armShare: { aaaa1111xx: [0.5, 0.7, 0.9], bbbb2222xx: [0.5, 0.3, 0.1] },
  perSegment: {
    mobile_young: {
      optimalArm: "bbbb2222xx",
      policies: {
        linear_ts: { pctOptimal: 0.8, avgReward: 0.05 },
        ucb1: { pctOptimal: 0.4, avgReward: 0.04 },
        uniform: { pctOptimal: 0.5, avgReward: 0.03 },
        oracle: { pctOptimal: 1, avgReward: 0.06 },
      },
    },
  },
  arms: [
    { creativeId: "bbbb2222xx", impressions: 100, estimatedCtr: 0.03, trueCtr: 0.035 },
    { creativeId: "aaaa1111xx", impressions: 900, estimatedCtr: 0.06, trueCtr: 0.058 },
  ],
};

describe("series shaping", () => {
  it("orders policies linear_ts first and oracle last", () => {
    expect(orderPolicies(["oracle", "uniform", "zeta", "linear_ts", "ucb1"])).toEqual([
      "linear_ts",
      "ucb1",
      "uniform",
      "zeta",
      "oracle",
    ]);
  });
  it("builds curve series with the oracle as a dashed reference", () => {
    const s = curveSeries(METRICS, "cumAvgReward", { includeOracle: true });
    expect(s.map((x) => x.id)).toEqual(["linear_ts", "ucb1", "uniform", "oracle"]);
    const oracle = s[3];
    expect(oracle.reference).toBe(true);
    expect(oracle.dash).toBeTruthy();
    expect(s[0].points).toEqual([
      { x: 10, y: 0.03 },
      { x: 100, y: 0.04 },
      { x: 1000, y: 0.05 },
    ]);
    expect(s[0].band).toBeUndefined();
  });
  it("drops the oracle and adds CI bands for regret", () => {
    const s = curveSeries(METRICS, "cumRegret", { bands: true });
    expect(s.map((x) => x.id)).not.toContain("oracle");
    expect(s[0].band?.[2]).toEqual({ x: 1000, lo: 8.9, hi: 9.1 });
  });
  it("uses distinct colours per policy", () => {
    const s = curveSeries(METRICS, "cumAvgReward", { includeOracle: true });
    expect(new Set(s.map((x) => x.color)).size).toBe(s.length);
  });
  it("builds arm share series in arm order with stable colours", () => {
    const s = armShareSeries(METRICS, ARMS);
    expect(s.map((x) => x.id)).toEqual(["aaaa1111xx", "bbbb2222xx"]);
    expect(s[0].label).toBe("The Golden Golf Cart Gig");
    expect(s[0].color).toBe(armColor(ARMS, "aaaa1111xx"));
    expect(s[1].color).toBe(armColor(ARMS, "bbbb2222xx"));
  });
  it("finds per-segment winners and the best baseline (never the oracle)", () => {
    expect(segmentRows(METRICS, ARMS)).toEqual([
      {
        segment: "mobile_young",
        optimalArm: "The Jackpot Reveal",
        linearTs: 0.8,
        bestBaseline: { policy: "uniform", label: "Uniform random", pctOptimal: 0.5 },
      },
    ]);
  });
  it("orders total bars and arm stats", () => {
    expect(totalBars(METRICS).map((b) => b.id)).toEqual(["linear_ts", "uniform", "oracle"]);
    const rows = armStatRows(METRICS, ARMS);
    expect(rows.map((r) => r.name)).toEqual(["The Golden Golf Cart Gig", "The Jackpot Reveal"]);
    expect(rows[0].impressions).toBe(900);
  });
  it("detects whether there is anything to chart", () => {
    expect(hasMetrics(METRICS)).toBe(true);
    expect(hasMetrics({ ...METRICS, episodes: 0 })).toBe(false);
    expect(hasMetrics(null)).toBe(false);
  });
});
