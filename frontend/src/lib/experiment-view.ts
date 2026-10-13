/**
 * Experiment page view state: the Overview | Analysis switch (in the URL as
 * `?view=analysis`), the persisted "Explain" preference (localStorage `tt:explain`),
 * and the page's metrics refresh loop + status-poll identity helper.
 */
import { useCallback, useSyncExternalStore } from "react";
import type { ExperimentSummary } from "./experiments";

export type ExperimentView = "overview" | "analysis";
export const VIEWS: readonly ExperimentView[] = ["overview", "analysis"];

/** `?view=` value → view; anything unknown is the Overview (the default). */
export function parseView(value: string | null | undefined): ExperimentView {
  return value === "analysis" ? "analysis" : "overview";
}

/** The current URL with `view` set (the default view drops the param), other params kept. */
export function urlForView(href: string, view: ExperimentView): string {
  const url = new URL(href);
  if (view === "overview") url.searchParams.delete("view");
  else url.searchParams.set("view", view);
  return `${url.pathname}${url.search}${url.hash}`;
}

/** `?creative=` value → the open creative's id, or null (drawer closed). */
export function parseCreativeParam(value: string | null | undefined): string | null {
  const v = (value ?? "").trim();
  return /^[A-Za-z0-9_-]{1,128}$/.test(v) ? v : null;
}

/** The current URL with `creative` set (null drops it), other params (e.g. `view`) kept. */
export function urlForCreative(href: string, creativeId: string | null): string {
  const url = new URL(href);
  if (creativeId) url.searchParams.set("creative", creativeId);
  else url.searchParams.delete("creative");
  return `${url.pathname}${url.search}${url.hash}`;
}

export const EXPLAIN_KEY = "tt:explain";
const EXPLAIN_EVENT = "tt:explain-change";

type ReadStore = Pick<Storage, "getItem"> | null | undefined;
type WriteStore = Pick<Storage, "setItem" | "removeItem"> | null | undefined;

/** Stored preference; off unless explicitly "1". Storage errors (privacy mode) read as off. */
export function readExplainPref(storage: ReadStore): boolean {
  try {
    return storage?.getItem(EXPLAIN_KEY) === "1";
  } catch {
    return false;
  }
}

/** Persist the preference ("1" or removed). Returns false if storage refused it. */
export function writeExplainPref(storage: WriteStore, on: boolean): boolean {
  try {
    if (!storage) return false;
    if (on) storage.setItem(EXPLAIN_KEY, "1");
    else storage.removeItem(EXPLAIN_KEY);
    return true;
  } catch {
    return false;
  }
}

const browserStorage = () => (typeof window === "undefined" ? null : window.localStorage);

function subscribe(onChange: () => void): () => void {
  const onStorage = (e: StorageEvent) => {
    if (e.key === null || e.key === EXPLAIN_KEY) onChange();
  };
  window.addEventListener("storage", onStorage);
  window.addEventListener(EXPLAIN_EVENT, onChange);
  return () => {
    window.removeEventListener("storage", onStorage);
    window.removeEventListener(EXPLAIN_EVENT, onChange);
  };
}

/** The Explain preference, synced across tabs; off during SSR and the first paint. */
export function useExplainPref(): [boolean, (on: boolean) => void] {
  const on = useSyncExternalStore(
    subscribe,
    () => readExplainPref(browserStorage()),
    () => false
  );
  const set = useCallback((next: boolean) => {
    writeExplainPref(browserStorage(), next);
    window.dispatchEvent(new Event(EXPLAIN_EVENT));
  }, []);
  return [on, set];
}

// ── Polling helpers for the experiment page ──────────────────────────────────

/** Metrics refresh interval while traffic runs (measured from the end of the previous load). */
export const METRICS_INTERVAL_MS = 10_000;
/** A metrics load slower than this is abandoned (the loop carries on). */
export const METRICS_TIMEOUT_MS = 15_000;

const abortReason = (signal: AbortSignal) => signal.reason ?? new DOMException("Aborted", "AbortError");

/**
 * Run `fn` bounded by `timeoutMs` and `signal`. The same controller + setTimeout +
 * race pattern as the status poll's `getExperimentWithTimeout` in
 * `lib/experiments.ts`, which is private to that module and bound to the status
 * GET, so it can't be reused for the metrics + series pair. Raced as well, so a
 * fetch that ignores its signal can't hang the loop.
 */
async function withTimeout<T>(fn: (signal: AbortSignal) => Promise<T>, timeoutMs: number, signal: AbortSignal): Promise<T> {
  if (signal.aborted) throw abortReason(signal);
  const ctrl = new AbortController();
  const onAbort = () => ctrl.abort(abortReason(signal));
  signal.addEventListener("abort", onAbort, { once: true });
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timedOut = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      const err = new Error(`The metrics request timed out after ${Math.round(timeoutMs / 1000)} s`);
      ctrl.abort(err);
      reject(err);
    }, timeoutMs);
  });
  try {
    return await Promise.race([fn(ctrl.signal), timedOut]);
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", onAbort);
  }
}

/** Sleep `ms`; resolves early (without waiting the rest) when `signal` aborts. */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(t);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const t = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
  });
}

/**
 * Load the metrics once, then (with `repeat`) again `intervalMs` after each load
 * finishes: a self-scheduling loop, so a slow response never overlaps the next
 * request (a fixed setInterval stacked them). Each load is bounded by
 * `timeoutMs`; a failed or timed-out load is skipped (the last good value
 * stays). Every request gets a sequence number and only the newest one issued,
 * while the loop is live, may `apply`, so a stale response can never overwrite a
 * newer one. Returns `stop` (unmount / status change), which aborts the
 * in-flight request and drops anything that lands afterwards.
 */
export function startMetricsLoop<T>(opts: {
  fetch: (signal: AbortSignal) => Promise<T>;
  apply: (value: T) => void;
  repeat: boolean;
  intervalMs?: number;
  timeoutMs?: number;
}): () => void {
  const intervalMs = opts.intervalMs ?? METRICS_INTERVAL_MS;
  const timeoutMs = opts.timeoutMs ?? METRICS_TIMEOUT_MS;
  const ctrl = new AbortController();
  let seq = 0;
  const loadOnce = async () => {
    const id = ++seq;
    try {
      const value = await withTimeout(opts.fetch, timeoutMs, ctrl.signal);
      if (!ctrl.signal.aborted && id === seq) opts.apply(value);
    } catch {
      // Timed out, aborted or failed: keep the last good value.
    }
  };
  void (async () => {
    await loadOnce();
    while (opts.repeat && !ctrl.signal.aborted) {
      await sleep(intervalMs, ctrl.signal);
      if (ctrl.signal.aborted) break;
      await loadOnce();
    }
  })();
  return () => ctrl.abort();
}

/**
 * The status poll's next summary, or `prev` itself when nothing it is read for
 * changed (same `updatedAt`, `status` and `progress`): every poll parses a new
 * object, and keeping the old one stops the page's derived memos (insights,
 * shift preview, …) recomputing every 5 s.
 */
export function keepIfUnchanged(prev: ExperimentSummary | null, next: ExperimentSummary): ExperimentSummary {
  if (!prev) return next;
  const sameProgress =
    prev.progress?.episodesDone === next.progress?.episodesDone &&
    prev.progress?.episodesTotal === next.progress?.episodesTotal;
  return prev.updatedAt === next.updatedAt && prev.status === next.status && sameProgress ? prev : next;
}
