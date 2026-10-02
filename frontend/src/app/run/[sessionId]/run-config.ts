/**
 * If a run is still "running" but no new event has arrived for this long, we
 * surface a "may have stalled" state. The async-job model polls a detached run,
 * so an orphaned job (e.g. an Agent Engine instance recycle) would otherwise
 * report "running" forever with no events. Reset on every new event.
 */
export const RUN_STALL_TIMEOUT_MS = 3 * 60 * 1000;

/**
 * While paused at a review checkpoint, how often the run page re-checks the run
 * for an answer submitted elsewhere (another tab, or a reload that resumed it).
 * Without it a stale tab shows "Waiting for review" forever.
 */
export const PAUSE_WATCH_INTERVAL_MS = 10_000;

/**
 * Author of the server's internal run-status marker events (`__run_status`
 * done/error/running). These are control-plane events the poll payload already
 * reflects in its top-level `status`/`error` fields — the run's coarse status
 * comes from there, not from these events — so we skip them in the timeline and
 * state merge to keep the UI clean (they carry no agent output). Must match
 * `RUNSERVER_AUTHOR` in `runserver/async_runs.py`.
 */
export const RUNSERVER_MARKER_AUTHOR = "__runserver__";

/**
 * Pipeline state keys to surface as outputs on the run page, in pipeline order.
 * `noun` is the [singular, plural] item name used in the open-button label
 * (e.g. "4 ad copies").
 */
export const PIPELINE_STATE_KEYS: {
  key: string;
  label: string;
  noun: [string, string];
}[] = [
  { key: "ad_copy_critique", label: "Ad copy", noun: ["ad copy", "ad copies"] },
  {
    key: "final_visual_concepts",
    label: "Visual concepts",
    noun: ["visual concept", "visual concepts"],
  },
];

/** Human-readable labels for schema field keys. */
export const FIELD_LABELS: Record<string, string> = {
  id: "ID",
  original_id: "ID",
  ad_copy_id: "Ad copy ID",
  tone_style: "Tone / style",
  headline: "Headline",
  body_text: "Body text",
  trend_connection: "Trend connection",
  audience_appeal_rationale: "Audience appeal",
  audience_appeal: "Audience appeal",
  social_caption: "Social caption",
  call_to_action: "Call to action",
  detailed_performance_rationale: "Performance rationale",
  selection_rationale: "Selection rationale",
  concept_name: "Concept name",
  trend_visual_link: "Trend visual link",
  trend: "Trend",
  trend_reference: "Trend reference",
  markets_product: "Markets product",
  concept_summary: "Concept summary",
  image_generation_prompt: "Image prompt",
  critique_summary: "Critique summary",
};

/** Fields to hide from item cards. */
export const HIDDEN_FIELDS = new Set(["id", "original_id", "ad_copy_id"]);

/** Per-widget layout config: side-by-side pairs + full-width field. */
export const WIDGET_LAYOUTS: Record<string, { pairs: [string, string][]; fullWidth: string }> = {
  final_visual_concepts: {
    pairs: [
      ["trend", "trend_reference"],
      ["markets_product", "audience_appeal"],
      ["selection_rationale", "social_caption"],
      ["call_to_action", "concept_summary"],
    ],
    fullWidth: "image_generation_prompt",
  },
  ad_copy_critique: {
    pairs: [
      ["tone_style", "call_to_action"],
      ["trend_connection", "body_text"],
      ["audience_appeal_rationale", "social_caption"],
    ],
    fullWidth: "detailed_performance_rationale",
  },
};

/** Default layout for unknown widget types. */
export const DEFAULT_LAYOUT = { pairs: [] as [string, string][], fullWidth: "" };
