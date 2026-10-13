/**
 * The timely creative's expiry: one pure rule shared with the api's share and
 * experiment guards (runserver/timely.py), pinned by the golden fixture
 * tests/fixtures/timely_expiry.json that both test suites read.
 *
 * - Timely: `state.trend_situation.timely_ok === true` with a non-empty
 *   `valid_until`, and the concept's `uses_trend_state === true` or its paired
 *   ad copy's (concept `ad_copy_id` == copy `original_id`, compared as strings).
 * - Expired: timely, and `valid_until` (an ISO datetime with an offset) is at or
 *   before `now`. An unparseable `valid_until` never expires.
 *
 * `now` is the wall clock (ms since the epoch), never the run's
 * `current_datetime`: a resumed session keeps its original run date.
 */

export interface TimelyStatus {
  timely: boolean;
  expired: boolean;
  /** `trend_situation.valid_until` as written ("" when not timely). */
  validUntilIso: string;
  /** "Timely", "Expired" or "" (not timely). */
  label: "Timely" | "Expired" | "";
}

const NOT_TIMELY: TimelyStatus = { timely: false, expired: false, validUntilIso: "", label: "" };

type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);

/** A state value that may be a JSON string (as some session stores keep it). */
function parseObj(raw: unknown): Obj | null {
  if (typeof raw === "string") {
    try {
      raw = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  return isObj(raw) ? raw : null;
}

function listOf(raw: unknown, key: string): Obj[] {
  const list = parseObj(raw)?.[key];
  return Array.isArray(list) ? list.filter(isObj) : [];
}

const hasId = (id: unknown): id is string | number =>
  (typeof id === "string" && id !== "") || (typeof id === "number" && Number.isFinite(id));

/** The ad copy paired with a concept by id (never by headline or position). */
export function pairedCopy<T extends { original_id?: unknown }>(
  copies: readonly T[],
  concept: { ad_copy_id?: unknown },
): T | undefined {
  const id = concept.ad_copy_id;
  if (!hasId(id)) return undefined;
  return copies.find((c) => hasId(c.original_id) && String(c.original_id) === String(id));
}

/** `valid_until` of a timely moment, or "" when the run has none. */
export function timelyValidUntil(trendSituation: unknown): string {
  const s = parseObj(trendSituation);
  if (!s || s.timely_ok !== true) return "";
  return typeof s.valid_until === "string" ? s.valid_until.trim() : "";
}

const ISO_WITH_OFFSET = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/;

/** Epoch ms of an ISO datetime with an offset, or null when unparseable. */
export function parseValidUntil(iso: string): number | null {
  if (!ISO_WITH_OFFSET.test(iso)) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

/** True when `validUntilIso` parses and is at or before `now`. */
export function isExpiredAt(validUntilIso: string, now: number): boolean {
  const ms = parseValidUntil(validUntilIso);
  return ms !== null && ms <= now;
}

/** The status of one concept given its paired copy and the run's moment. */
export function timelyStatusFor(
  concept: { uses_trend_state?: unknown },
  copy: { uses_trend_state?: unknown } | undefined,
  trendSituation: unknown,
  now: number,
): TimelyStatus {
  const validUntilIso = timelyValidUntil(trendSituation);
  if (!validUntilIso) return NOT_TIMELY;
  if (concept.uses_trend_state !== true && copy?.uses_trend_state !== true) return NOT_TIMELY;
  const expired = isExpiredAt(validUntilIso, now);
  return { timely: true, expired, validUntilIso, label: expired ? "Expired" : "Timely" };
}

/** The timely status of one concept, read straight from session state. */
export function timelyStatus(state: Obj | null | undefined, conceptName: string, now: number): TimelyStatus {
  if (!state) return NOT_TIMELY;
  const concept = listOf(state.final_visual_concepts, "visual_concepts").find(
    (c) => c.concept_name === conceptName,
  );
  if (!concept) return NOT_TIMELY;
  const copy = pairedCopy(listOf(state.ad_copy_critique, "ad_copies"), concept);
  return timelyStatusFor(concept, copy, state.trend_situation, now);
}

/** True when the concept is the run's timely creative (expired or not). */
export function isTimely(state: Obj | null | undefined, conceptName: string): boolean {
  return timelyStatus(state, conceptName, 0).timely;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/**
 * "Oct 14, 23:59" for an ISO datetime, read in its own offset (the run's
 * timezone, as the validator wrote it), or null when it doesn't look like one.
 */
export function formatValidUntil(iso: string): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})/.exec(iso);
  if (!m) return null;
  const month = MONTHS[Number(m[2]) - 1];
  if (!month) return null;
  return `${month} ${Number(m[3])}, ${m[4]}`;
}
