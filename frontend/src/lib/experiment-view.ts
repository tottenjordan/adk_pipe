/**
 * Experiment page view state: the Overview | Analysis switch (in the URL as
 * `?view=analysis`) and the persisted "Explain" preference (localStorage `tt:explain`).
 */
import { useCallback, useSyncExternalStore } from "react";

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
