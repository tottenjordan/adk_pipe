import { PendingLongRunningCalls, type PauseContext } from "@/lib/pause-detection";
import {
  answeredReviewNames,
  deriveStages,
  isPopulated,
  type RunStageStatus,
} from "@/lib/run-stages";
import type { AgentEvent } from "@/lib/types";

/**
 * "Stopped early" detection: the backend run segment ended (`done`) but the
 * workflow never wrote its final key, and no review checkpoint is waiting. This
 * happens when the root model returns an empty turn mid-workflow. Recovery is a
 * new user message on the same session ({@link CONTINUE_MESSAGE}); the agent
 * picks up from its history.
 */

/** The message posted to the same session to continue a run that stopped early. */
export const CONTINUE_MESSAGE =
  "Continue the WORKFLOW from where it stopped. Do not repeat completed steps; call the next step now.";

/**
 * The state keys that mark each agent's run as finished; any one populated
 * means done. Creative apps finish at `finalize_done` (set by finalize_pipeline's
 * terminal node on every path, even when the eval report save fails), falling
 * back to `eval_report_gcs_uri` for sessions that predate `finalize_done`.
 */
export const FINAL_COMPLETION_KEYS: Record<string, readonly string[]> = {
  trend_scout: ["select_trends_markdown_gcs_uri"],
  creative_agent: ["finalize_done", "eval_report_gcs_uri"],
  interactive_creative: ["finalize_done", "eval_report_gcs_uri"],
};

/**
 * Non-null when the run's segment completed, no review is pending, and the
 * app's final completion keys are all missing. `stage` is the label of the first
 * stage that didn't finish (where the run stopped). Unknown apps → null (no
 * known final key, so "stopped early" can't be told apart from done).
 */
export function stoppedEarly(
  appName: string,
  state: Record<string, unknown>,
  status: RunStageStatus,
  pause: PauseContext | null,
  answeredReviews: ReadonlySet<string> = new Set()
): { stage: string } | null {
  if (status !== "completed" || pause) return null;
  const finalKeys = FINAL_COMPLETION_KEYS[appName];
  if (!finalKeys || finalKeys.some((key) => isPopulated(state[key]))) return null;
  const stages = deriveStages(appName, state, null, "completed", answeredReviews);
  const open = stages.find((s) => s.state !== "done" && s.state !== "degraded");
  // The final key is missing, so its stage is open at the latest.
  return { stage: open?.label ?? stages[stages.length - 1].label };
}

/**
 * {@link stoppedEarly} for a finished session read after the fact (results
 * page): the pause and answered reviews come from the session's event log, and
 * the segment is assumed ended. A review still waiting is not "stopped early".
 */
export function sessionStoppedEarly(
  appName: string,
  state: Record<string, unknown>,
  events: AgentEvent[]
): { stage: string } | null {
  const pending = new PendingLongRunningCalls();
  for (const ev of events) pending.observe(ev);
  return stoppedEarly(appName, state, "completed", pending.pause(), answeredReviewNames(events));
}

/**
 * True when the run produced visual concepts but never rendered images (the
 * flag `generate_image` sets is unset), so image URLs would only 404.
 */
export function imagesNotRendered(state: Record<string, unknown>): boolean {
  return isPopulated(state.final_visual_concepts) && !isPopulated(state._images_generated);
}
