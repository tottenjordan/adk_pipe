"use client";

import { useRef, useState, type Dispatch, type SetStateAction } from "react";

/**
 * In-memory drafts of the interactive review panels, keyed by the paused
 * checkpoint's function-call id. The run page unmounts the panel while a resume
 * is in flight, so a resume the server rejects (400 invalid edits, or a 409
 * "not applied") would otherwise remount it empty and lose the user's edits.
 * Page-lifetime only (a reload starts fresh); one entry per checkpoint call.
 */
const drafts = new Map<string, unknown>();

/** `useState` whose value outlives a remount for the same `key` (no key → plain state). */
export function useReviewDraft<T>(
  key: string | undefined,
  init: () => T
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() =>
    key && drafts.has(key) ? (drafts.get(key) as T) : init()
  );
  // The latest value set, so functional updates chain within one event without
  // a state updater (updaters must be pure; saving the draft is a side effect).
  const latest = useRef(value);
  const set: Dispatch<SetStateAction<T>> = (next) => {
    const resolved = typeof next === "function" ? (next as (p: T) => T)(latest.current) : next;
    latest.current = resolved;
    if (key) drafts.set(key, resolved);
    setValue(resolved);
  };
  return [value, set];
}

/**
 * Forget the drafts of one checkpoint call (every key `<functionCallId>:…`).
 * Called once its resume was accepted: the panel never remounts for that call
 * again, so its drafts would otherwise live for the whole page.
 */
export function clearDraftsFor(functionCallId: string): void {
  const prefix = `${functionCallId}:`;
  for (const key of [...drafts.keys()]) {
    if (key.startsWith(prefix)) drafts.delete(key);
  }
}

/** Test hook: forget every saved draft. */
export function clearReviewDrafts(): void {
  drafts.clear();
}
