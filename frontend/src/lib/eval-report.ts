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
