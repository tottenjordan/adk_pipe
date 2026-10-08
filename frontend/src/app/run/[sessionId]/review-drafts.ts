"use client";

import { useState, type Dispatch, type SetStateAction } from "react";

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
  const set: Dispatch<SetStateAction<T>> = (next) => {
    setValue((prev) => {
      const resolved = typeof next === "function" ? (next as (p: T) => T)(prev) : next;
      if (key) drafts.set(key, resolved);
      return resolved;
    });
  };
  return [value, set];
}

/** Test hook: forget every saved draft. */
export function clearReviewDrafts(): void {
  drafts.clear();
}
