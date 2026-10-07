import type { PauseContext } from "@/lib/pause-detection";
import { imagesRetryExhausted } from "@/lib/utils";

/**
 * The run page's stage spine, derived purely from session state.
 *
 * Each agent writes a known state key when a step finishes (its ADK
 * `output_key`, or a tool's `*_gcs_uri`), so the spine is a fixed ordered list
 * per agent whose progress is read straight off `sessionState`. No events are
 * parsed here except the optional set of answered review checkpoints, which the
 * page collects from function responses (review stages write no state key).
 */

export type StageState = "done" | "active" | "needs_review" | "pending" | "degraded";

export interface Stage {
  id: string;
  label: string;
  state: StageState;
}

/** The page's coarse run status (mirrors the run page's `Status`). */
export type RunStageStatus = "running" | "completed" | "error" | "paused" | "stalled";

interface StageDef {
  id: string;
  label: string;
  /** State key whose population marks the stage done. */
  key?: string;
  /** Review checkpoint tool name (`LongRunningFunctionTool`). */
  review?: string;
}

const CREATIVE_HEAD: StageDef[] = [
  { id: "research", label: "Research", key: "combined_final_cited_report" },
  // Written inside the research pipeline, before the report PDF (which renders
  // it). Sessions from before the brief never write it; the stage still shows
  // done once any later stage is done (backward propagation in deriveStages).
  { id: "brief", label: "Brief", key: "creative_brief" },
  { id: "research_report", label: "Research report", key: "research_report_gcs_uri" },
];
const AD_COPY: StageDef = { id: "ad_copy", label: "Ad copy", key: "ad_copy_critique" };
const VISUALS: StageDef = {
  id: "visual_concepts",
  label: "Visual concepts",
  key: "final_visual_concepts",
};
const CREATIVE_TAIL: StageDef[] = [
  { id: "images", label: "Images", key: "_images_generated" },
  { id: "eval_save", label: "Evaluation & save", key: "eval_report_gcs_uri" },
];

const CREATIVE_STAGES: StageDef[] = [...CREATIVE_HEAD, AD_COPY, VISUALS, ...CREATIVE_TAIL];

const INTERACTIVE_STAGES: StageDef[] = [
  ...CREATIVE_HEAD,
  { id: "review_research", label: "Review brief", review: "review_research" },
  AD_COPY,
  { id: "review_ad_copy", label: "Review ad copy", review: "review_ad_copies" },
  VISUALS,
  { id: "review_visuals", label: "Review visuals", review: "review_visual_concepts" },
  ...CREATIVE_TAIL,
];

function trendScoutStages(state: Record<string, unknown>): StageDef[] {
  const pick: StageDef[] = state.interactive_trend_pick
    ? [{ id: "pick_trends", label: "Pick trends", key: "target_search_trends", review: "review_trends" }]
    : [];
  return [
    { id: "gather_trends", label: "Gather trends", key: "raw_gtrends" },
    ...pick,
    { id: "research", label: "Research", key: "info_gtrends" },
    { id: "write_strategy", label: "Write strategy", key: "selected_gtrends" },
    { id: "save", label: "Save", key: "select_trends_markdown_gcs_uri" },
  ];
}

/** Fallback for an app the page doesn't know: one generic stage. */
const UNKNOWN_STAGES: StageDef[] = [{ id: "run", label: "Run" }];

/** One-line plain description of what each stage does, for the current-stage panel. */
export const STAGE_DESCRIPTIONS: Record<string, string> = {
  gather_trends: "Fetching today's top Google Search trends.",
  pick_trends: "Choose which trends to research.",
  research: "Searching the web for context on the trend and the campaign.",
  write_strategy: "Picking the most relevant trends and writing the strategy.",
  save: "Saving the trend picks.",
  brief: "Writing the creative brief: one proposition, the trend fit and the angles.",
  research_report: "Writing the cited research report.",
  review_research: "Review the creative brief and research before ad copy is written.",
  ad_copy: "Drafting and critiquing ad copy.",
  review_ad_copy: "Review the ad copy before visual concepts are drafted.",
  visual_concepts: "Drafting and critiquing visual concepts.",
  review_visuals: "Review the visual concepts before images are rendered.",
  images: "Rendering images for each visual concept.",
  eval_save: "Scoring every creative and saving the outputs.",
  run: "The agent is working.",
};

/**
 * True when a state value holds real output. Agents seed some keys with empty
 * shells (e.g. `target_search_trends: { target_search_trends: [] }`), so nested
 * containers count only when something inside them does.
 */
export function isPopulated(value: unknown): boolean {
  if (value == null) return false;
  if (typeof value === "string") return value.trim() !== "";
  if (typeof value === "boolean") return value;
  if (Array.isArray(value)) return value.some(isPopulated);
  if (typeof value === "object") return Object.values(value).some(isPopulated);
  return true;
}

const EXHAUSTED_SUFFIX = "__retry_exhausted";

/**
 * The truthy retry-exhaustion markers, split per stage.
 *
 * - `own`: the stage's own producer gave up (`<stage key>__retry_exhausted`,
 *   e.g. `_images_generated`, `creative_brief`, trend_scout's `info_gtrends`),
 *   so the stage has ended — with no output.
 * - `sub`: a sub-step inside the stage gave up (e.g. the gs/campaign searchers
 *   inside Research). The stage keeps running (refinement, the composer) and
 *   may still produce its output, so such a marker never ends the stage.
 *   Unknown markers map to the brief (`creative_brief*`) or else to research.
 *
 * Falsy markers (`null`: cleared by a later successful retry) are ignored.
 */
function exhaustionMarkers(
  defs: StageDef[],
  state: Record<string, unknown>
): { own: Set<string>; sub: Set<string> } {
  const own = new Set<string>();
  const sub = new Set<string>();
  const byKey = new Map(defs.filter((d) => d.key).map((d) => [d.key as string, d.id]));
  if (imagesRetryExhausted(state)) own.add("images");
  for (const [key, value] of Object.entries(state)) {
    if (!key.endsWith(EXHAUSTED_SUFFIX) || !value) continue;
    const stageKey = key.slice(0, -EXHAUSTED_SUFFIX.length);
    const ownId = byKey.get(stageKey);
    if (ownId) own.add(ownId);
    else if (stageKey.startsWith("_images_generated")) own.add("images");
    else sub.add(stageKey.startsWith("creative_brief") ? "brief" : "research");
  }
  return { own, sub };
}

/**
 * Derive the ordered stage spine for a run.
 *
 * - A stage is done when its key is populated, its review was answered, or any
 *   later stage is done (agents can skip writing an optional key).
 * - "degraded" = the stage finished WITHOUT its own output, with a retry
 *   marker to explain it: its own producer's marker (which also ends the stage,
 *   so the next one becomes active), or a sub-step marker once a later stage is
 *   done or the run completed. A sub-step marker alone never ends a stage, and
 *   a stage whose output exists is never degraded (the retry recovered).
 * - running / stalled: the first unfinished stage is "active".
 * - paused: the paused review stage is "needs_review" (always — the live pause
 *   wins over later data); nothing else is active.
 * - error: the first unfinished stage stays "active" (it's where the run
 *   stopped); the page shows the error and drops the processing animation.
 * - completed: stages with no data stay "pending" — completion is not faked.
 */
export function deriveStages(
  appName: string,
  state: Record<string, unknown>,
  pause: PauseContext | null,
  status: RunStageStatus,
  answeredReviews: ReadonlySet<string> = new Set()
): Stage[] {
  const defs =
    appName === "trend_scout"
      ? trendScoutStages(state)
      : appName === "creative_agent"
        ? CREATIVE_STAGES
        : appName === "interactive_creative"
          ? INTERACTIVE_STAGES
          : UNKNOWN_STAGES;

  const markers = exhaustionMarkers(defs, state);
  const ownDone = defs.map(
    (d) =>
      (d.key !== undefined && isPopulated(state[d.key])) ||
      (d.review !== undefined && answeredReviews.has(d.review) && pause?.functionName !== d.review) ||
      (d.id === "run" && status === "completed")
  );

  // Done-ness propagates backwards: anything before a finished stage finished.
  const finished = new Array<boolean>(defs.length).fill(false);
  const degraded = new Array<boolean>(defs.length).fill(false);
  let laterDone = false;
  for (let i = defs.length - 1; i >= 0; i--) {
    const gaveUp = !ownDone[i] && markers.own.has(defs[i].id);
    degraded[i] =
      !ownDone[i] &&
      (gaveUp || (markers.sub.has(defs[i].id) && (laterDone || status === "completed")));
    finished[i] = ownDone[i] || laterDone || gaveUp;
    if (ownDone[i] || laterDone) laterDone = true;
  }

  const pausedIndex =
    status === "paused" && pause ? defs.findIndex((d) => d.review === pause.functionName) : -1;
  const firstOpen = finished.indexOf(false);

  return defs.map((d, i) => {
    let s: StageState = "pending";
    // The live pause is authoritative, even if stale later data says otherwise.
    if (i === pausedIndex) s = "needs_review";
    else if (degraded[i]) s = "degraded";
    else if (finished[i]) s = "done";
    else if (
      i === firstOpen &&
      (status === "running" || status === "stalled" || status === "error")
    )
      s = "active";
    return { id: d.id, label: d.label, state: s };
  });
}

/** The stage the current-stage panel describes: needs review, else active, else last finished. */
export function currentStage(stages: Stage[]): Stage | null {
  return (
    stages.find((s) => s.state === "needs_review") ??
    stages.find((s) => s.state === "active") ??
    [...stages].reverse().find((s) => s.state === "done" || s.state === "degraded") ??
    null
  );
}

/** Review checkpoint tool names answered in `events` (function responses). */
export function answeredReviewNames(
  events: { content?: { parts?: { functionResponse?: { name?: string } }[] } }[]
): Set<string> {
  const names = new Set<string>();
  for (const ev of events) {
    for (const part of ev.content?.parts ?? []) {
      const name = part.functionResponse?.name;
      if (name && name.startsWith("review_")) names.add(name);
    }
  }
  return names;
}
