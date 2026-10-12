import { getEventError } from "./api";
import type { AgentEvent } from "./types";

/**
 * Non-fatal step errors on the run page.
 *
 * An error event in the polled log (errorCode / errorMessage) does not mean the
 * run failed: a fail-soft step (FailSoftNode) contains its model error and the
 * pipeline carries on, but the failing agent's error event stays in the log.
 * Only the poll payload's status (the server's terminal marker) decides
 * error / done, so these are shown as a muted notice instead.
 */

/** At most this many distinct step errors are kept for the notice. */
export const STEP_ERRORS_MAX = 5;

export const STEP_ERROR_PREFIX = "A step reported an error and was skipped: ";

/** The readable error carried by an event (429 → friendly quota text), or null. */
export function stepErrorMessage(event: AgentEvent): string | null {
  return getEventError(event);
}

/** Append `message` unless already listed or the list is full (same list back
 *  when unchanged, so a React state update is a no-op). */
export function addStepError(list: string[], message: string): string[] {
  if (list.includes(message) || list.length >= STEP_ERRORS_MAX) return list;
  return [...list, message];
}
