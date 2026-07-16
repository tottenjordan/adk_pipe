/**
 * If a run is still "running" but no new event has arrived for this long, we
 * surface a "may have stalled" state. The async-job model polls a detached run,
 * so an orphaned job (e.g. an Agent Engine instance recycle) would otherwise
 * report "running" forever with no events. Reset on every new event.
 */
export const RUN_STALL_TIMEOUT_MS = 3 * 60 * 1000;

/**
 * Author of the server's internal run-status marker events (`__run_status`
 * done/error/running). These are control-plane events the poll payload already
 * reflects in its top-level `status`/`error` fields — the run's coarse status
 * comes from there, not from these events — so we skip them in the timeline and
 * state merge to keep the UI clean (they carry no agent output). Must match
 * `RUNSERVER_AUTHOR` in `runserver/async_runs.py`.
 */
export const RUNSERVER_MARKER_AUTHOR = "__runserver__";

/** Pipeline state keys to surface as collapsible widgets, in display order (newest first). */
export const PIPELINE_STATE_KEYS = [
  { key: "final_visual_concepts", label: "Final Visual Concepts" },
  { key: "ad_copy_critique", label: "Ad Copy Critique" },
];

/** Human-readable labels for schema field keys. */
export const FIELD_LABELS: Record<string, string> = {
  id: "ID",
  original_id: "ID",
  ad_copy_id: "Ad Copy ID",
  tone_style: "Tone / Style",
  headline: "Headline",
  body_text: "Body Text",
  trend_connection: "Trend Connection",
  audience_appeal_rationale: "Audience Appeal",
  audience_appeal: "Audience Appeal",
  social_caption: "Social Caption",
  call_to_action: "Call to Action",
  detailed_performance_rationale: "Performance Rationale",
  selection_rationale: "Selection Rationale",
  concept_name: "Concept Name",
  trend_visual_link: "Trend Visual Link",
  trend: "Trend",
  trend_reference: "Trend Reference",
  markets_product: "Markets Product",
  concept_summary: "Concept Summary",
  image_generation_prompt: "Image Prompt",
  critique_summary: "Critique Summary",
};

/** Color accent for specific field labels to make key fields pop. */
export const FIELD_COLORS: Record<string, string> = {
  headline: "text-indigo-600",
  body_text: "text-indigo-500",
  concept_name: "text-violet-600",
  concept_summary: "text-violet-500",
  call_to_action: "text-amber-600",
  social_caption: "text-amber-500",
  trend_connection: "text-emerald-600",
  trend_visual_link: "text-emerald-600",
  trend: "text-emerald-600",
  trend_reference: "text-emerald-500",
  audience_appeal: "text-pink-600",
  audience_appeal_rationale: "text-pink-600",
  markets_product: "text-pink-500",
  tone_style: "text-violet-500",
  image_generation_prompt: "text-cyan-600",
  critique_summary: "text-orange-600",
  selection_rationale: "text-orange-500",
  detailed_performance_rationale: "text-orange-500",
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
