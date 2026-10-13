/**
 * Fetches the creative evaluation report from the /api/gcs proxy.
 *
 * The report (`creative_eval_report.json`) is the LAST artifact the creative pipeline
 * writes — it lands seconds after the images/PDF and after the run stream reports
 * "completed". So the results page can mount and fetch it *before* it exists, getting
 * a 404. Rather than swallow that miss, we retry on 404 (and on transient network
 * errors) and surface a distinct "pending" outcome the UI can show + let the user
 * refresh.
 */
import type { CreativeScore, EvalReport } from "@/lib/eval-matching";

type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const strings = (v: unknown): string[] | undefined =>
  Array.isArray(v) ? v.filter((s): s is string => typeof s === "string") : undefined;
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** A score with the lists the UI maps over defaulted; null without a numeric overall score. */
function parseScore(raw: unknown): CreativeScore | null {
  if (!isObj(raw) || typeof raw.overall_score !== "number" || !Number.isFinite(raw.overall_score)) {
    return null;
  }
  const score = {
    ...raw,
    verdicts: list(raw.verdicts),
    strengths: strings(raw.strengths) ?? [],
    improvements: strings(raw.improvements) ?? [],
  } as CreativeScore & Obj;
  if (!Array.isArray(raw.gates)) delete score.gates;
  return score;
}

/** Entries that are objects with a usable score (others are dropped). */
function parseEntries<T>(raw: unknown): T[] {
  const out: T[] = [];
  for (const entry of list(raw)) {
    if (!isObj(entry)) continue;
    const score = parseScore(entry.score);
    if (score) out.push({ ...entry, score } as T);
  }
  return out;
}

/**
 * Normalise a fetched `creative_eval_report.json` so the results page can't throw on
 * a malformed or older report: evaluation lists default to [], entries without a
 * numeric score are dropped, the summary's numbers default to 0. A non-object or a
 * report without a summary object is unusable → null (the page's "no evaluation" state).
 */
export function parseEvalReport(raw: unknown): EvalReport | null {
  if (!isObj(raw) || !isObj(raw.summary)) return null;
  const s = raw.summary;
  const report = {
    ...raw,
    brand: str(raw.brand),
    target_product: str(raw.target_product),
    target_search_trend: str(raw.target_search_trend),
    ad_copy_evaluations: parseEntries(raw.ad_copy_evaluations),
    visual_concept_evaluations: parseEntries(raw.visual_concept_evaluations),
    summary: {
      ...s,
      total_ad_copies: num(s.total_ad_copies),
      ad_copies_passed: num(s.ad_copies_passed),
      avg_ad_copy_score: num(s.avg_ad_copy_score),
      total_visual_concepts: num(s.total_visual_concepts),
      visual_concepts_passed: num(s.visual_concepts_passed),
      avg_visual_score: num(s.avg_visual_score),
      overall_pass_rate: num(s.overall_pass_rate),
      weakest_dimensions: strings(s.weakest_dimensions) ?? [],
    },
  } as EvalReport & Obj;
  for (const key of ["warnings", "learning_flags"] as const) {
    const value = strings(raw[key]);
    if (value) report[key] = value;
    else delete report[key];
  }
  return report;
}

export interface FetchEvalOptions {
  /** Retries after the first attempt (default 5). Total attempts = retries + 1. */
  retries?: number;
  /** Base backoff between attempts in ms (default 2000). */
  delayMs?: number;
  /** Injectable fetch (tests). Defaults to global fetch. */
  fetchImpl?: typeof fetch;
  /** Injectable sleep (tests). Defaults to setTimeout-based delay. */
  sleep?: (ms: number) => Promise<void>;
}

export type EvalFetchResult<T> =
  | { status: "found"; report: T }
  | { status: "pending" }
  | { status: "error"; message: string };

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export async function fetchEvalReport<T = unknown>(
  url: string,
  opts: FetchEvalOptions = {}
): Promise<EvalFetchResult<T>> {
  const {
    retries = 5,
    delayMs = 2000,
    fetchImpl = fetch,
    sleep = defaultSleep,
  } = opts;

  const attempts = retries + 1;

  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      const res = await fetchImpl(url);
      if (res.ok) {
        return { status: "found", report: (await res.json()) as T };
      }
      // 404 = report not written yet (race). Any other status is a real failure.
      if (res.status !== 404) {
        return { status: "error", message: `GCS error: ${res.status}` };
      }
    } catch {
      // Transient network error — treat like a 404 and retry.
    }

    // Not the last attempt → wait, then retry.
    if (attempt < attempts - 1) {
      await sleep(delayMs);
    }
  }

  return { status: "pending" };
}
