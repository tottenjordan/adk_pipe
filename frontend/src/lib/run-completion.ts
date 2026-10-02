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

/** The state key each agent writes last; its absence means the run didn't finish. */
export const FINAL_COMPLETION_KEY: Record<string, string> = {
  trend_scout: "select_trends_markdown_gcs_uri",
  creative_agent: "eval_report_gcs_uri",
  interactive_creative: "eval_report_gcs_uri",
};

/**
 * Non-null when the run's segment completed, no review is pending, and the
 * app's final completion key is missing. `stage` is the label of the first
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
  const finalKey = FINAL_COMPLETION_KEY[appName];
  if (!finalKey || isPopulated(state[finalKey])) return null;
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
