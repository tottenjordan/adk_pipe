import { SELF_USER_ID } from "@/lib/api";
import { agentInfo, agentLabel, isAgentId, isCreativeAgent, type AgentId } from "@/lib/agents";
import type { CampaignInput, Session } from "@/lib/types";
import { referenceRowsFromState } from "@/lib/reference-images";
import { formatStateValue } from "@/lib/utils";

/**
 * Run history, derived purely from session state.
 *
 * Backend fact: every app shares one session store, so `listSessions(app, user)`
 * returns the same sessions whatever app name is asked for, and `appName` just
 * echoes the request. History therefore lists once and infers each session's
 * agent from its state (see {@link inferAgent}).
 */

/** The app name history lists sessions under (any app returns the same set). */
export const HISTORY_LIST_APP = "trend_scout";

/** UI-only state key recording which agent the home form started. */
export const UI_APP_KEY = "ui_app";

export type RunStatus =
  | "Running"
  | "Failed"
  | "Completed"
  | "Needs review"
  | "Incomplete"
  | "Not started";

export interface RunRow {
  id: string;
  /** Inferred app name, or null when the agent can't be identified. */
  app: AgentId | null;
  agentLabel: string;
  brand: string;
  trend: string;
  /** Last update, ms since the epoch (null when the backend didn't send one). */
  updatedAt: number | null;
  status: RunStatus;
  href: string;
  /** Home-form values for "Duplicate brief". */
  brief: Brief;
}

/** The session state key holding each agent's final deliverable. */
const FINAL_KEY: Record<AgentId, string> = {
  trend_scout: "select_trends_markdown_gcs_uri",
  creative_agent: "eval_report_gcs_uri",
  interactive_creative: "eval_report_gcs_uri",
};

/**
 * A run with no `__run_status` marker but recent activity is treated as running
 * for this long. Fresh kick-offs don't write a marker until they finish, so this
 * covers the in-flight case; it matches the backend's 30-minute run timeout.
 */
export const RUN_ACTIVE_WINDOW_MS = 30 * 60 * 1000;

/**
 * Which agent produced a session. `ui_app` (written by the home form since P1)
 * wins; older sessions fall back to the agent's output directory, which can't
 * tell the two creative agents apart, so those read as a plain "Creative run".
 */
export function inferAgent(state: Record<string, unknown>): AgentId | null {
  const uiApp = state[UI_APP_KEY];
  if (isAgentId(uiApp)) return uiApp;
  const dir = state.agent_output_dir;
  if (dir === "trawler_output") return "trend_scout";
  if (dir === "creative_output") return "creative_agent";
  return null;
}

function hasValue(value: unknown): boolean {
  if (value == null) return false;
  if (typeof value === "string") return value.trim() !== "";
  return true;
}

function hasFinalOutput(state: Record<string, unknown>, app: AgentId | null): boolean {
  if (app) return hasValue(state[FINAL_KEY[app]]);
  return Object.values(FINAL_KEY).some((k) => hasValue(state[k]));
}

function canPause(state: Record<string, unknown>, app: AgentId | null): boolean {
  if (app === "trend_scout") return Boolean(state.interactive_trend_pick);
  return app ? (agentInfo(app)?.canPause ?? false) : false;
}

/**
 * Status rules (state-only — list responses carry no events):
 * - `__run_status` "running" → Running; "error" → Failed.
 * - "done" → Completed when the agent's final key exists; otherwise Needs review
 *   if the run can pause for the user (trend_scout with the trend pick on, or
 *   interactive_creative) — a finished segment without output is a pause — else
 *   Incomplete.
 * - No marker (runs started before the async-job model, or a fresh run that has
 *   not finished its first segment): Completed if the final key exists; Running
 *   if the agent seeded its state (`agent_output_dir`) and the session was
 *   updated within {@link RUN_ACTIVE_WINDOW_MS}; Incomplete if it seeded state
 *   longer ago; Not started when the agent never ran.
 */
export function deriveStatus(
  state: Record<string, unknown>,
  app: AgentId | null,
  updatedAt: number | null,
  now: number,
): RunStatus {
  const marker = state.__run_status;
  if (marker === "running") return "Running";
  if (marker === "error") return "Failed";
  const done = hasFinalOutput(state, app);
  if (marker === "done") {
    if (done) return "Completed";
    return canPause(state, app) ? "Needs review" : "Incomplete";
  }
  if (done) return "Completed";
  if (!hasValue(state.agent_output_dir)) return "Not started";
  if (updatedAt != null && now - updatedAt < RUN_ACTIVE_WINDOW_MS) return "Running";
  return "Incomplete";
}

const QUOTED = /'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"/g;

/**
 * Normalise `target_search_trends` into a readable, comma-separated string. It
 * may be a plain string, a list, a `{target_search_trends: [...]}` wrapper, or
 * that wrapper's Python-repr string (`"{'target_search_trends': ['a', 'b']}"`).
 */
export function formatTrend(value: unknown): string {
  if (typeof value === "string") {
    const s = value.trim();
    if (!/^[[{]/.test(s)) return s;
    const items = [...s.matchAll(QUOTED)]
      .map((m) => (m[1] ?? m[2] ?? "").trim())
      .filter((t) => t && t !== "target_search_trends");
    return items.join(", ");
  }
  return formatStateValue(value);
}

function str(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function runHref(id: string, app: AgentId | null, status: RunStatus): string {
  const params = new URLSearchParams({
    app: app ?? HISTORY_LIST_APP,
    userId: SELF_USER_ID,
  });
  const page = status === "Completed" && isCreativeAgent(app) ? "results" : "run";
  return `/${page}/${encodeURIComponent(id)}?${params.toString()}`;
}

export function toRunRow(session: Session, now: number): RunRow {
  const state = session.state ?? {};
  const app = inferAgent(state);
  const updatedAt =
    typeof session.lastUpdateTime === "number" ? session.lastUpdateTime * 1000 : null;
  const status = deriveStatus(state, app, updatedAt, now);
  return {
    id: session.id,
    app,
    agentLabel: agentLabel(app),
    brand: str(state.brand),
    trend: formatTrend(state.target_search_trends ?? state.target_search_trend),
    updatedAt,
    status,
    href: runHref(session.id, app, status),
    brief: briefFromState(state),
  };
}

/** Rows for a session list, newest first (sessions without a time sort last). */
export function buildRunRows(sessions: Session[], now: number): RunRow[] {
  return sessions
    .map((s) => toRunRow(s, now))
    .sort((a, b) => (b.updatedAt ?? -Infinity) - (a.updatedAt ?? -Infinity));
}

/** Short, scannable time: "Just now", "12 min ago", "3 h ago", "Yesterday", "Sep 28". */
export function formatRunTime(updatedAt: number | null, now: number): string {
  if (updatedAt == null) return "";
  const diff = Math.max(0, now - updatedAt);
  const min = Math.floor(diff / 60_000);
  if (min < 1) return "Just now";
  if (min < 60) return `${min} min ago`;
  const hours = Math.floor(min / 60);
  if (hours < 24) return `${hours} h ago`;
  if (hours < 48) return "Yesterday";
  const date = new Date(updatedAt);
  const sameYear = date.getFullYear() === new Date(now).getFullYear();
  return date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

/** The brief a "Duplicate brief" action carries back to the home form. */
export type Brief = Partial<CampaignInput>;

/** Visual-intent state keys → form fields (mirrors buildInitialState). */
const BRIEF_STATE_FIELDS: Array<[string, keyof CampaignInput]> = [
  ["brand", "brand"],
  ["target_audience", "targetAudience"],
  ["target_product", "targetProduct"],
  ["key_selling_points", "keySellingPoints"],
  ["visual_intent", "visualIntent"],
  ["brand_colors", "brandColors"],
  ["visual_style_preference", "visualStylePreference"],
  ["visual_avoid", "visualAvoid"],
  ["visual_aspect_ratio", "visualAspectRatio"],
];

/** Home-form values recovered from a past session's state (empty keys omitted). */
export function briefFromState(state: Record<string, unknown>): Brief {
  const brief: Brief = {};
  const app = inferAgent(state);
  if (app) brief.agent = app;
  for (const [key, field] of BRIEF_STATE_FIELDS) {
    const value = str(state[key]);
    if (value) (brief as Record<string, unknown>)[field] = value;
  }
  const trend = formatTrend(state.target_search_trends ?? state.target_search_trend);
  if (trend) brief.targetSearchTrend = trend;
  if (state.interactive_trend_pick === true) brief.interactiveTrendPick = true;
  if (state.learn_from_ratings === true) brief.learnFromRatings = true;
  // The chosen person (the form drops it if the consent was revoked since).
  const person = state.person_reference;
  if (person && typeof person === "object") {
    const { uri, consent_id: consentId } = person as Record<string, unknown>;
    if (typeof uri === "string" && uri && typeof consentId === "string" && consentId) {
      brief.personReference = { uri, consentId };
    }
  }
  // Reference rows: the legacy pair (or, when empty, the first listed
  // reference) is row 1; the rest are the extra rows.
  const references = referenceRowsFromState(state);
  if (references.first) {
    brief.referenceImageUri = references.first.uri;
    brief.referenceImageRole = references.first.role;
  }
  if (references.extras.length) brief.extraReferenceImages = references.extras;
  return brief;
}

/** sessionStorage key carrying a duplicated brief to the home form. */
export const DUPLICATE_BRIEF_KEY = "tt:duplicate-brief";

export function stashDuplicateBrief(brief: Brief): void {
  try {
    sessionStorage.setItem(DUPLICATE_BRIEF_KEY, JSON.stringify(brief));
  } catch {
    // sessionStorage unavailable — the home form simply opens empty.
  }
}

/** Read and clear a stashed brief (one-shot, so a reload starts clean). */
export function takeDuplicateBrief(): Brief | null {
  try {
    const raw = sessionStorage.getItem(DUPLICATE_BRIEF_KEY);
    if (!raw) return null;
    sessionStorage.removeItem(DUPLICATE_BRIEF_KEY);
    const parsed: unknown = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? (parsed as Brief) : null;
  } catch {
    return null;
  }
}
