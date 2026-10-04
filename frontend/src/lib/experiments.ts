/**
 * Bandit experiments: types, REST client and pure display helpers.
 * The contract is docs/bandit/contracts.md §5 (routes, status enum, payload types).
 * Every call goes through the same-origin /api/adk proxy with the placeholder
 * userId "me"; the proxy substitutes the IAP-verified caller (user-scoping.ts).
 */
import { SELF_USER_ID } from "./api";
import { downsampleIndices, type Point } from "./chart";
import { gcsProxyUrl, parseGsUri } from "./gcs";
import { overridesFromValues, type TuneValues } from "./scenario-preview";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/adk";

// ── Contract types (§5) ──────────────────────────────────────────────────────

export type ExperimentStatus =
  | "deploying"
  | "ready"
  | "running_traffic"
  | "stopping"
  | "stopped"
  | "failed"
  | "expired";

export type Scenario = "clear_winner" | "segment_winners" | "drift";
export type CtrMode = "demo" | "realistic";
export type RewardMode = "click" | "engaged";
export type Policy =
  | "linear_ts"
  | "ucb1"
  | "epsilon_greedy"
  | "beta_bernoulli_ts"
  | "uniform"
  | "oracle";

/**
 * User-tuned tweaks to the scenario preset (contracts §9, REST camelCase). Every
 * field optional; only knobs that differ from the preset are sent.
 */
export type ScenarioOverrides = {
  /** One weight per segment (clear_winner/drift 3, segment_winners 4), each in [0.05, 1]. */
  segmentMix?: number[];
  /** [0.25, 2]: how far apart the creatives are. */
  gapScale?: number;
  /** [0, 1]: 0 judge right, 0.5 no information, 1 judge backwards. */
  judgeWrong?: number;
  /** [0, 2]: multiplies the simulator's random variation. */
  noiseScale?: number;
  /** [0.2, 0.8]: drift change point as a fraction of the run (drift only). */
  driftAtFrac?: number;
};

export type Arm = {
  creativeId: string;
  index: number;
  label: string;
  conceptName: string;
  imageUri: string | null;
  scores: Record<string, number>;
  overallScore: number | null;
};

export type ExperimentSummary = {
  experimentId: string;
  userId: string;
  sessionId: string;
  appName: string;
  createdAt: string;
  updatedAt: string;
  status: ExperimentStatus;
  scenario: string;
  ctrMode: string;
  rewardMode: string;
  ttlExpiresAt: string | null;
  arms: Arm[];
  endpointId: string | null;
  trafficExecution: string | null;
  progress: { episodesDone: number; episodesTotal: number } | null;
  error: string | null;
  /** Set only when the experiment was deployed with tuned readers (absent from older APIs). */
  scenarioOverrides?: ScenarioOverrides | null;
};

/** Mean ± 95% CI across episodes, one value per checkpoint. */
export type Band = { mean: number[]; lo: number[]; hi: number[] };

export type ExperimentMetrics = {
  experimentId: string;
  episodes: number;
  horizon: number | null;
  checkpoints: number[];
  /** Order: linear_ts first, oracle last. */
  policies: string[];
  curves: Record<string, { cumAvgReward: Band; cumRegret: Band; pctOptimal: Band }>;
  /** Expected total reward ± std per policy. */
  totals: Record<string, { mean: number; std: number }>;
  /** linear_ts only, by creativeId: share of pulls in each checkpoint window. */
  armShare: Record<string, number[]>;
  perSegment: Record<
    string,
    { optimalArm: string; policies: Record<string, { pctOptimal: number; avgReward: number }> }
  >;
  arms: { creativeId: string; impressions: number; estimatedCtr: number; trueCtr: number }[];
};

/** One creative's per-window performance under the live endpoint (contracts §8). */
export type CreativeSeriesItem = {
  creativeId: string;
  /** Share of the endpoint's impressions in each window (sums to ~1 across creatives). */
  share: number[];
  /** Observed click rate per window; null where the creative had no impressions. */
  ctr: (number | null)[];
  cumClicks: number[];
  impressions: number;
  clicks: number;
  trueCtr: number | null;
  /** Segments where this creative is the optimal arm. */
  segmentsWon: string[];
  finalShare: number;
  /** Per audience segment, sorted by segment name; the same list for every creative (absent from older APIs). */
  segments?: CreativeSegmentStat[];
  /** Mean per episode: expected clicks lost vs the best creative for the readers this one was shown to. */
  missedClicks?: number;
  /** Engaged seconds per 1,000 impressions; only for reward_mode "engaged", else null. */
  engagedSecondsPer1k?: number | null;
};

/** One creative's results with one audience segment (contracts §8). */
export type CreativeSegmentStat = {
  segment: string;
  impressions: number;
  clicks: number;
  /** Observed clicks / impressions; null with no impressions. */
  ctr: number | null;
  /** Simulator truth for this creative with this segment. */
  trueCtr: number | null;
  /** This creative is the best choice for the segment. */
  isBest: boolean;
};

/** `GET …/creatives`: 20 equal round windows; creatives ordered by finalShare desc. */
export type CreativeSeries = {
  experimentId: string;
  episodes: number;
  horizon: number | null;
  windows: { start: number; end: number }[];
  creatives: CreativeSeriesItem[];
};

export interface CreateExperimentRequest {
  userId: string;
  appName: string;
  sessionId: string;
  creativeIndices: number[];
  scenario: Scenario;
  ctrMode: CtrMode;
  rewardMode: RewardMode;
  ttlMinutes?: number;
  scenarioOverrides?: ScenarioOverrides;
}

// ── Errors ───────────────────────────────────────────────────────────────────

/**
 * Thrown by `createExperiment` on 409 `active_experiment`: the caller already has
 * a live experiment (limit one per user, for cost). Nothing was deployed; the
 * caller should point the user at their experiments list to stop the other one.
 */
export class ActiveExperimentError extends Error {
  constructor(
    message = "You already have a live experiment. Stop it before deploying another one."
  ) {
    super(message);
    this.name = "ActiveExperimentError";
  }
}

/** A non-OK response from the experiments API, with the HTTP status kept for callers. */
export class ExperimentApiError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message);
    this.name = "ExperimentApiError";
  }
}

/** Machine-readable `detail.reason` (or a string `detail`) from an error body. */
async function errorDetail(res: Response): Promise<{ reason: string | null; text: string }> {
  const text = await res.text().catch(() => "");
  try {
    const detail = JSON.parse(text)?.detail;
    if (typeof detail === "string") return { reason: null, text: detail };
    const reason = typeof detail?.reason === "string" ? detail.reason : null;
    const message = typeof detail?.message === "string" ? detail.message : text;
    return { reason, text: message };
  } catch {
    return { reason: null, text };
  }
}

async function fail(res: Response, what: string): Promise<never> {
  const { text } = await errorDetail(res);
  throw new ExperimentApiError(`${what} (${res.status})${text ? `: ${text}` : ""}`, res.status);
}

const JSON_HEADERS = { "Content-Type": "application/json" };
const experimentUrl = (id: string) =>
  `${API_BASE}/experiments/${SELF_USER_ID}/${encodeURIComponent(id)}`;

// ── REST client ──────────────────────────────────────────────────────────────

/** `POST /experiments` → 201 `{experimentId, status: "deploying"}`. */
export async function createExperiment(
  body: CreateExperimentRequest
): Promise<{ experimentId: string; status: ExperimentStatus }> {
  const res = await fetch(`${API_BASE}/experiments`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  if (res.status === 409) {
    const { reason, text } = await errorDetail(res);
    if (reason === "active_experiment") throw new ActiveExperimentError();
    throw new ExperimentApiError(`Couldn't deploy the experiment (409)${text ? `: ${text}` : ""}`, 409);
  }
  if (!res.ok) return fail(res, "Couldn't deploy the experiment");
  return res.json();
}

/** `GET /experiments/{user}` → newest first. */
export async function listExperiments(): Promise<ExperimentSummary[]> {
  const res = await fetch(`${API_BASE}/experiments/${SELF_USER_ID}`);
  if (!res.ok) return fail(res, "Couldn't load experiments");
  const data = await res.json();
  return Array.isArray(data?.experiments) ? data.experiments : [];
}

/** `GET /experiments/{user}/{id}` — status reconciled against the endpoint and TTL. */
export async function getExperiment(
  experimentId: string,
  opts: { signal?: AbortSignal } = {}
): Promise<ExperimentSummary> {
  const res = await fetch(experimentUrl(experimentId), { signal: opts.signal });
  if (!res.ok) return fail(res, "Couldn't load the experiment");
  return res.json();
}

/** `GET /experiments/{user}/{id}/metrics` — `episodes: 0` until traffic runs. */
export async function getExperimentMetrics(
  experimentId: string,
  opts: { signal?: AbortSignal } = {}
): Promise<ExperimentMetrics> {
  const res = await fetch(`${experimentUrl(experimentId)}/metrics`, { signal: opts.signal });
  if (!res.ok) return fail(res, "Couldn't load the metrics");
  return res.json();
}

/** `GET /experiments/{user}/{id}/creatives` (contracts §8): per-creative series for the scoreboard. */
export async function getCreativeSeries(
  experimentId: string,
  opts: { signal?: AbortSignal } = {}
): Promise<CreativeSeries> {
  const res = await fetch(`${experimentUrl(experimentId)}/creatives`, { signal: opts.signal });
  if (!res.ok) return fail(res, "Couldn't load the creative series");
  const data = await res.json();
  return {
    ...data,
    windows: Array.isArray(data?.windows) ? data.windows : [],
    creatives: Array.isArray(data?.creatives) ? data.creatives : [],
  };
}

/** `POST …/traffic {episodes, horizon?}` — 409 unless the experiment is `ready`. */
export async function startTraffic(
  experimentId: string,
  episodes: number,
  horizon?: number
): Promise<{ status: ExperimentStatus; execution?: string }> {
  const res = await fetch(`${experimentUrl(experimentId)}/traffic`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(horizon ? { episodes, horizon } : { episodes }),
  });
  if (res.status === 409) {
    throw new ExperimentApiError(
      "Traffic can only start while the endpoint is ready. Wait for the current step to finish.",
      409
    );
  }
  if (!res.ok) return fail(res, "Couldn't start traffic");
  return res.json();
}

/** `POST …/stop` → `{status: "stopping" | "stopped"}`. */
export async function stopExperiment(experimentId: string): Promise<{ status: ExperimentStatus }> {
  const res = await fetch(`${experimentUrl(experimentId)}/stop`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: "{}",
  });
  if (!res.ok) return fail(res, "Couldn't stop the experiment");
  return res.json();
}

/** Statuses that change on their own, so the page keeps polling. */
export const NON_TERMINAL_STATUSES: readonly ExperimentStatus[] = [
  "deploying",
  "running_traffic",
  "stopping",
];

export function isNonTerminal(status: string): boolean {
  return (NON_TERMINAL_STATUSES as readonly string[]).includes(status);
}

/**
 * Poll an experiment, yielding each summary, until its status settles (anything
 * but deploying / running_traffic / stopping). Throws on an HTTP error; pass
 * `opts.signal` to cancel on unmount. Restart it after `startTraffic`/`stopExperiment`.
 */
export async function* pollExperiment(
  experimentId: string,
  opts: { intervalMs?: number; signal?: AbortSignal } = {}
): AsyncGenerator<ExperimentSummary> {
  const intervalMs = opts.intervalMs ?? 5000;
  for (;;) {
    const summary = await getExperiment(experimentId, { signal: opts.signal });
    yield summary;
    if (!isNonTerminal(summary.status)) return;
    await new Promise<void>((resolve, reject) => {
      const t = setTimeout(resolve, intervalMs);
      opts.signal?.addEventListener(
        "abort",
        () => {
          clearTimeout(t);
          reject(opts.signal?.reason ?? new DOMException("Aborted", "AbortError"));
        },
        { once: true }
      );
    });
  }
}

// ── Labels ───────────────────────────────────────────────────────────────────

export type StatusTone = "pass" | "fail" | "pending" | "active" | "muted";

const STATUS_INFO: Record<ExperimentStatus, { label: string; tone: StatusTone }> = {
  deploying: { label: "Deploying", tone: "active" },
  ready: { label: "Ready", tone: "pass" },
  running_traffic: { label: "Running traffic", tone: "active" },
  stopping: { label: "Stopping", tone: "active" },
  stopped: { label: "Stopped", tone: "muted" },
  failed: { label: "Failed", tone: "fail" },
  expired: { label: "Expired", tone: "muted" },
};

/** Sentence-case label and tone for a status (unknown statuses render as-is, muted). */
export function statusInfo(status: string): { label: string; tone: StatusTone } {
  return (
    STATUS_INFO[status as ExperimentStatus] ?? {
      label: status ? status.charAt(0).toUpperCase() + status.slice(1).replace(/_/g, " ") : "Unknown",
      tone: "muted",
    }
  );
}

/** Tailwind classes per tone: dot fill + label text. */
export const TONE_CLASSES: Record<StatusTone, { dot: string; text: string }> = {
  pass: { dot: "bg-mark-pass", text: "text-mark-pass" },
  fail: { dot: "bg-mark-fail", text: "text-mark-fail" },
  pending: { dot: "bg-mark-pending", text: "text-mark-pending" },
  active: { dot: "bg-primary", text: "text-foreground" },
  muted: { dot: "bg-muted-foreground/40", text: "text-muted-foreground" },
};

/** Whether the experiment still holds (or is acquiring) an endpoint, i.e. can be stopped. */
export function canStop(status: string): boolean {
  return status === "deploying" || status === "ready" || status === "running_traffic";
}

/** "1 h 25 min left" / "12 min left" / "Less than a minute left" / "Expired"; "" with no TTL. */
export function ttlText(ttlExpiresAt: string | null | undefined, now: number): string {
  if (!ttlExpiresAt) return "";
  const t = Date.parse(ttlExpiresAt);
  if (Number.isNaN(t)) return "";
  const ms = t - now;
  if (ms <= 0) return "Expired";
  const min = Math.floor(ms / 60_000);
  if (min < 1) return "Less than a minute left";
  if (min < 60) return `${min} min left`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return m ? `${h} h ${m} min left` : `${h} h left`;
}

const POLICY_LABELS: Record<Policy, string> = {
  linear_ts: "Linear Thompson sampling (endpoint)",
  ucb1: "UCB",
  epsilon_greedy: "ε-greedy",
  beta_bernoulli_ts: "Thompson sampling (no context)",
  uniform: "Uniform random",
  oracle: "Oracle",
};

/** Shorter names for tight spots (direct line labels, bar rows). */
const POLICY_SHORT: Record<Policy, string> = {
  linear_ts: "Linear TS",
  ucb1: "UCB",
  epsilon_greedy: "ε-greedy",
  beta_bernoulli_ts: "TS (no context)",
  uniform: "Uniform",
  oracle: "Oracle",
};

export function policyLabel(policy: string): string {
  return POLICY_LABELS[policy as Policy] ?? policy;
}

export function policyShortLabel(policy: string): string {
  return POLICY_SHORT[policy as Policy] ?? policy;
}

export const SCENARIO_OPTIONS: { value: Scenario; label: string }[] = [
  { value: "clear_winner", label: "Clear winner" },
  { value: "segment_winners", label: "Segment-specific winners" },
  { value: "drift", label: "Drift" },
];

export const CTR_MODE_OPTIONS: { value: CtrMode; label: string }[] = [
  { value: "demo", label: "Demo" },
  { value: "realistic", label: "Realistic" },
];

export const REWARD_MODE_OPTIONS: { value: RewardMode; label: string }[] = [
  { value: "click", label: "Click" },
  { value: "engaged", label: "Engaged time" },
];

export const TTL_OPTIONS = [60, 120, 240] as const;
export const EPISODE_OPTIONS = [5, 20, 50] as const;

const labelFrom = <T extends string>(opts: { value: T; label: string }[], v: string) =>
  opts.find((o) => o.value === v)?.label ?? v;

export const scenarioLabel = (s: string) => labelFrom(SCENARIO_OPTIONS, s);
export const ctrModeLabel = (m: string) =>
  m === "demo" ? "Demo click rates" : m === "realistic" ? "Realistic click rates" : m;
export const rewardModeLabel = (m: string) =>
  m === "click" ? "Click reward" : m === "engaged" ? "Engaged-time reward" : m;

/** Default rounds per episode (the scenario presets: 20k / 40k / 40k demo; ~10× in realistic). */
export function defaultHorizon(scenario: string, ctrMode: string): number {
  const demo = scenario === "clear_winner" ? 20_000 : 40_000;
  return ctrMode === "realistic" ? Math.min(demo * 10, 400_000) : demo;
}

/** First 8 characters of a creative id (cards and tables). */
export function shortId(id: string): string {
  return id.slice(0, 8);
}

/** Same-origin image URL for an arm's `gs://` image, or null. */
export function armImageUrl(arm: Pick<Arm, "imageUri">): string | null {
  const parsed = parseGsUri(arm.imageUri);
  return parsed ? gcsProxyUrl(parsed.bucket, parsed.path) : null;
}

/** Segment keys arrive snake_case ("mobile_young"); show them as words ("Mobile young"). */
export function segmentLabel(s: string): string {
  const t = s.replace(/[_-]+/g, " ").trim();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

/** Short name for an arm in charts and tables: the concept name (headlines run long). */
export function armName(arm: Pick<Arm, "label" | "conceptName" | "creativeId">): string {
  return arm.conceptName || arm.label || shortId(arm.creativeId);
}

// ── Deploy panel ─────────────────────────────────────────────────────────────

export interface DeploySelection {
  appName: string;
  sessionId: string;
  /** Pipeline indices (into final_visual_concepts) of the chosen creatives. */
  selected: Iterable<number>;
  scenario: Scenario;
  ctrMode: CtrMode;
  rewardMode: RewardMode;
  ttlMinutes: number;
  /** The Advanced sliders; only values that differ from the scenario preset are sent. */
  tuning?: TuneValues;
}

/** The deploy panel's choices → the `POST /experiments` body (indices sorted, de-duplicated). */
export function selectionToPayload(sel: DeploySelection): CreateExperimentRequest {
  const creativeIndices = [...new Set(sel.selected)]
    .filter((i) => Number.isInteger(i) && i >= 0)
    .sort((a, b) => a - b);
  const body: CreateExperimentRequest = {
    userId: SELF_USER_ID,
    appName: sel.appName,
    sessionId: sel.sessionId,
    creativeIndices,
    scenario: sel.scenario,
    ctrMode: sel.ctrMode,
    rewardMode: sel.rewardMode,
    ttlMinutes: sel.ttlMinutes,
  };
  const overrides = sel.tuning ? overridesFromValues(sel.scenario, sel.tuning) : undefined;
  return overrides ? { ...body, scenarioOverrides: overrides } : body;
}

/** True when an experiment ran with tuned readers (at least one override set). */
export function hasOverrides(ov: ScenarioOverrides | null | undefined): ov is ScenarioOverrides {
  return Boolean(ov && Object.values(ov).some((v) => v !== undefined && v !== null));
}

/** Why the Deploy button is disabled, or null when deploying is allowed. */
export function deployBlockedReason(selectedCount: number, stoppedEarly: boolean): string | null {
  if (stoppedEarly) return "This run stopped early, so its creatives weren't all evaluated. Finish the run first.";
  if (selectedCount < 2) return "Pick at least two creatives: the bandit needs something to compare.";
  if (selectedCount > 4) return "Pick at most four creatives.";
  return null;
}

// ── Chart series shaping ─────────────────────────────────────────────────────

/**
 * Categorical policy palette (validated with the dataviz skill's validator, light
 * surface, all pairs: worst CVD ΔE 9.7, normal-vision ΔE 15.3). It avoids the
 * reserved pass/fail/pending status hues. Uniform (the floor) and oracle (the
 * ceiling) are neutral references, told apart by dash pattern.
 */
export const POLICY_COLORS: Record<Policy, string> = {
  linear_ts: "#2a78d6",
  ucb1: "#ff7f50",
  epsilon_greedy: "#6b3fa6",
  beta_bernoulli_ts: "#d36fa6",
  uniform: "#7d858f",
  oracle: "#1a1d21",
};
const POLICY_DASH: Partial<Record<Policy, string>> = { uniform: "2 3", oracle: "6 4" };

/** Arm colours in arm order (the four chromatic slots; arms are capped at 4). */
// Creatives get their own earth-tone palette so a creative line never shares a
// colour with a strategy line (POLICY_COLORS): teal, ochre-brown, olive, navy.
export const ARM_COLORS = ["#0f766e", "#8a5a2b", "#5f7a1f", "#1e3a5f"] as const;

const CANONICAL_ORDER: Policy[] = [
  "linear_ts",
  "ucb1",
  "epsilon_greedy",
  "beta_bernoulli_ts",
  "uniform",
  "oracle",
];

/** linear_ts first, oracle last, known baselines in canonical order, unknown ones after them. */
export function orderPolicies(policies: string[]): string[] {
  const rank = (p: string) => {
    if (p === "oracle") return 1000;
    const i = CANONICAL_ORDER.indexOf(p as Policy);
    return i >= 0 ? i : 500;
  };
  return [...policies].sort((a, b) => rank(a) - rank(b));
}

export interface ChartSeries {
  id: string;
  label: string;
  shortLabel: string;
  color: string;
  dash?: string;
  /** Drawn as a quiet reference (oracle / optimum), no band. */
  reference?: boolean;
  points: Point[];
  band?: { x: number; lo: number; hi: number }[];
}

export type CurveKey = "cumAvgReward" | "cumRegret" | "pctOptimal";

const MAX_POINTS = 120;

/**
 * Per-policy series for one curve. `oracle` becomes a dashed reference line when
 * `includeOracle` (the cumulative-reward chart) and is dropped otherwise (its
 * regret is 0 and its % optimal 100 by definition).
 */
export function curveSeries(
  metrics: ExperimentMetrics,
  key: CurveKey,
  opts: { includeOracle?: boolean; bands?: boolean } = {}
): ChartSeries[] {
  const xs = metrics.checkpoints ?? [];
  const idx = downsampleIndices(xs.length, MAX_POINTS);
  return orderPolicies(metrics.policies ?? [])
    .filter((p) => metrics.curves?.[p]?.[key] && (p !== "oracle" || opts.includeOracle))
    .map((p) => {
      const band = metrics.curves[p][key];
      const reference = p === "oracle";
      return {
        id: p,
        label: policyLabel(p),
        shortLabel: policyShortLabel(p),
        color: POLICY_COLORS[p as Policy] ?? "#7d858f",
        dash: POLICY_DASH[p as Policy],
        reference,
        points: idx.map((i) => ({ x: xs[i], y: band.mean[i] })),
        band:
          opts.bands && !reference && band.lo?.length && band.hi?.length
            ? idx.map((i) => ({ x: xs[i], lo: band.lo[i], hi: band.hi[i] }))
            : undefined,
      };
    });
}

/** linear_ts arm share over time: one series per arm, coloured in arm order. */
export function armShareSeries(metrics: ExperimentMetrics, arms: Arm[]): ChartSeries[] {
  const xs = metrics.checkpoints ?? [];
  const idx = downsampleIndices(xs.length, MAX_POINTS);
  const ordered = [...arms].sort((a, b) => a.index - b.index);
  const known = new Set(ordered.map((a) => a.creativeId));
  const extra = Object.keys(metrics.armShare ?? {}).filter((id) => !known.has(id));
  const ids = [...ordered.map((a) => a.creativeId), ...extra];
  return ids
    .map((id, k) => ({ id, k }))
    .filter(({ id }) => metrics.armShare?.[id])
    .map(({ id, k }) => {
      const arm = ordered.find((a) => a.creativeId === id);
      const name = arm ? armName(arm) : shortId(id);
      return {
        id,
        label: name,
        shortLabel: name,
        color: ARM_COLORS[k % ARM_COLORS.length],
        points: idx.map((i) => ({ x: xs[i], y: metrics.armShare[id][i] })),
      };
    });
}

export interface SegmentRow {
  segment: string;
  optimalArm: string;
  linearTs: number | null;
  bestBaseline: { policy: string; label: string; pctOptimal: number } | null;
}

/** Per-segment winners: the optimal arm, linear_ts % optimal, and the best non-oracle baseline. */
export function segmentRows(metrics: ExperimentMetrics, arms: Arm[]): SegmentRow[] {
  const nameOf = (id: string) => {
    const arm = arms.find((a) => a.creativeId === id);
    return arm ? armName(arm) : shortId(id);
  };
  return Object.entries(metrics.perSegment ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([segment, seg]) => {
      let best: SegmentRow["bestBaseline"] = null;
      for (const [policy, v] of Object.entries(seg.policies ?? {})) {
        if (policy === "linear_ts" || policy === "oracle") continue;
        if (!best || v.pctOptimal > best.pctOptimal) {
          best = { policy, label: policyLabel(policy), pctOptimal: v.pctOptimal };
        }
      }
      return {
        segment,
        optimalArm: nameOf(seg.optimalArm),
        linearTs: seg.policies?.linear_ts?.pctOptimal ?? null,
        bestBaseline: best,
      };
    });
}

export interface TotalBar {
  id: string;
  label: string;
  color: string;
  mean: number;
  std: number;
}

/** Expected total reward ± std per policy, in policy order. */
export function totalBars(metrics: ExperimentMetrics): TotalBar[] {
  return orderPolicies(Object.keys(metrics.totals ?? {})).map((p) => ({
    id: p,
    label: policyLabel(p),
    color: POLICY_COLORS[p as Policy] ?? "#7d858f",
    mean: metrics.totals[p].mean,
    std: metrics.totals[p].std,
  }));
}

export interface ArmStatRow {
  creativeId: string;
  name: string;
  color: string;
  impressions: number;
  estimatedCtr: number;
  trueCtr: number;
}

/** Impressions and estimated vs true CTR per arm, in arm order. */
export function armStatRows(metrics: ExperimentMetrics, arms: Arm[]): ArmStatRow[] {
  const ordered = [...arms].sort((a, b) => a.index - b.index);
  const pos = (id: string) => {
    const i = ordered.findIndex((a) => a.creativeId === id);
    return i >= 0 ? i : ordered.length;
  };
  return [...(metrics.arms ?? [])]
    .sort((a, b) => pos(a.creativeId) - pos(b.creativeId))
    .map((row) => {
      const arm = ordered.find((a) => a.creativeId === row.creativeId);
      return {
        ...row,
        name: arm ? armName(arm) : shortId(row.creativeId),
        color: ARM_COLORS[pos(row.creativeId) % ARM_COLORS.length],
      };
    });
}

/** True when there is anything to chart (traffic has produced at least one episode). */
export function hasMetrics(metrics: ExperimentMetrics | null | undefined): metrics is ExperimentMetrics {
  return Boolean(metrics && metrics.episodes > 0 && metrics.checkpoints?.length);
}

/** The chart colour of an arm (by arm order), so cards, lines and table rows agree. */
export function armColor(arms: Arm[], creativeId: string): string {
  const ordered = [...arms].sort((a, b) => a.index - b.index);
  const i = ordered.findIndex((a) => a.creativeId === creativeId);
  return ARM_COLORS[(i >= 0 ? i : ordered.length) % ARM_COLORS.length];
}
