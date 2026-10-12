/**
 * The trend's validated current moment (`state.trend_situation`, written by
 * creative_agent's deterministic validator, creative_agent/trend_situation.py)
 * as a short read-only line for the run and results pages: a phase label
 * ("Game tomorrow" style: "Texas vs Oklahoma tomorrow", "Lost yesterday —
 * Texas vs Oklahoma", "Ongoing", or "Not timely: <reason>"), the number of
 * sources and, for a timely moment, until when it is valid.
 */

export type TrendPhase =
  | "upcoming"
  | "live"
  | "just_happened"
  | "ongoing"
  | "evergreen"
  | "sensitive"
  | "unknown";

const PHASES: readonly TrendPhase[] = [
  "upcoming",
  "live",
  "just_happened",
  "ongoing",
  "evergreen",
  "sensitive",
  "unknown",
];

export interface TrendSituation {
  phase: TrendPhase;
  eventName: string;
  eventStart: string;
  outcome: string;
  sourceIds: string[];
  asOf: string;
  timelyOk: boolean;
  validUntil: string;
  abstainReason: string;
}

export interface TrendSituationView {
  label: string;
  timely: boolean;
  sources: number;
  /** "YYYY-MM-DD HH:MM" in the run's timezone, timely moments only. */
  validUntil: string | null;
}

const str = (v: unknown): string => (typeof v === "string" ? v.trim() : "");

/** The validated situation from session state, or null when absent/invalid. */
export function parseTrendSituation(state: Record<string, unknown> | null | undefined): TrendSituation | null {
  const raw = state?.trend_situation;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const s = raw as Record<string, unknown>;
  const phase = str(s.phase) as TrendPhase;
  if (!PHASES.includes(phase)) return null;
  return {
    phase,
    eventName: str(s.event_name),
    eventStart: str(s.event_start),
    outcome: str(s.outcome),
    sourceIds: Array.isArray(s.source_ids) ? s.source_ids.filter((x): x is string => typeof x === "string") : [],
    asOf: str(s.as_of),
    timelyOk: s.timely_ok === true,
    validUntil: str(s.valid_until),
    abstainReason: str(s.abstain_reason),
  };
}

const OFFSET_RE = /(Z|[+-]\d{2}:\d{2})$/;

function offsetMinutes(iso: string): number {
  const m = OFFSET_RE.exec(iso);
  if (!m || m[1] === "Z") return 0;
  const sign = m[1].startsWith("-") ? -1 : 1;
  const [h, min] = m[1].slice(1).split(":").map(Number);
  return sign * (h * 60 + min);
}

/** Days since the epoch of an ISO date/datetime, read in the given UTC offset. */
function dayNumber(iso: string, offset: number): number | null {
  // A bare date, or a datetime without an offset, is already the run's local day.
  if (!OFFSET_RE.test(iso) || /^\d{4}-\d{2}-\d{2}$/.test(iso)) {
    const day = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
    return day ? Date.UTC(+day[1], +day[2] - 1, +day[3]) / 86_400_000 : null;
  }
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  return Math.floor((ms + offset * 60_000) / 86_400_000);
}

/** Event day minus the validation day (in the run's timezone), or null. */
export function daysFromAsOf(eventStart: string, asOf: string): number | null {
  if (!eventStart || !asOf) return null;
  const offset = offsetMinutes(asOf);
  const event = dayNumber(eventStart, offset);
  const today = dayNumber(asOf, offset);
  return event === null || today === null ? null : event - today;
}

function relativeDay(days: number | null): string {
  if (days === null) return "";
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days === -1) return "yesterday";
  return days > 0 ? `in ${days} days` : `${-days} days ago`;
}

const OUTCOME_VERBS: Record<string, string> = { win: "Won", loss: "Lost", draw: "Drew" };
const PHASE_LABELS: Record<TrendPhase, string> = {
  upcoming: "Upcoming",
  live: "Live now",
  just_happened: "Just happened",
  ongoing: "Ongoing",
  evergreen: "Evergreen",
  sensitive: "Sensitive",
  unknown: "Unknown",
};

const capitalize = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** The phase label, e.g. "Texas vs Oklahoma tomorrow" or "Lost yesterday — Texas vs Oklahoma". */
export function phaseLabel(s: TrendSituation): string {
  const when = relativeDay(daysFromAsOf(s.eventStart, s.asOf));
  if (s.phase === "upcoming" && when) {
    return s.eventName ? `${s.eventName} ${when}` : `Event ${when}`;
  }
  if (s.phase === "just_happened" && when) {
    const verb = OUTCOME_VERBS[s.outcome] ?? "Happened";
    return s.eventName ? `${verb} ${when} — ${s.eventName}` : `${verb} ${when}`;
  }
  return PHASE_LABELS[s.phase];
}

/** "YYYY-MM-DD HH:MM" of an ISO datetime as written (the run's timezone), or null. */
export function shortTime(iso: string): string | null {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
  return m ? `${m[1]} ${m[2]}` : null;
}

/** The view for the read-only line; null when absent or the feature is off. */
export function describeTrendSituation(
  state: Record<string, unknown> | null | undefined,
): TrendSituationView | null {
  const s = parseTrendSituation(state);
  if (!s || s.abstainReason === "disabled") return null;
  const sources = s.sourceIds.length;
  if (s.timelyOk) {
    return { label: phaseLabel(s), timely: true, sources, validUntil: shortTime(s.validUntil) };
  }
  const reason = s.abstainReason || "no validated moment";
  const label =
    s.phase === "unknown" ? `Not timely: ${reason}` : `${phaseLabel(s)} · not timely: ${reason}`;
  return { label: capitalize(label), timely: false, sources, validUntil: null };
}

/** The line's text after the "Current moment" label ("" when nothing to show). */
export function trendSituationText(state: Record<string, unknown> | null | undefined): string {
  const view = describeTrendSituation(state);
  if (!view) return "";
  const parts = [view.label];
  if (view.sources > 0) parts.push(`${view.sources} ${view.sources === 1 ? "source" : "sources"}`);
  if (view.validUntil) parts.push(`valid until ${view.validUntil}`);
  return parts.join(" · ");
}

/** True when the run's validated moment is a sensitive event (never referenced by the creatives). */
export function isSensitiveSituation(state: Record<string, unknown> | null | undefined): boolean {
  return parseTrendSituation(state)?.phase === "sensitive";
}
