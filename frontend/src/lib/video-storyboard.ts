/**
 * Phase 3 multi-cut video storyboard & generated reel helpers.
 *
 * Mirrors the Python `VideoShotSpec`, `VideoStoryboard`, and `generated_videos`
 * contracts (`creative_agent/schemas.py`, `creative_agent/video_qa.py`,
 * `creative_agent/video_tools.py`, and `runserver/async_runs.py`).
 */

export const SHOT_DURATION_OPTIONS = [4, 6, 8] as const;
export type ShotDurationSeconds = (typeof SHOT_DURATION_OPTIONS)[number];

export const MIN_STORYBOARD_SHOTS = 3;
export const MAX_STORYBOARD_SHOTS = 6;
export const HERO_DURATION_SECONDS = 30;
export const SOCIAL_MIN_SECONDS = 14;
export const SOCIAL_MAX_SECONDS = 16;
export const SOCIAL_MIN_SHOTS = 2;
export const BUMPER_DURATION_SECONDS = 6;
export const BUMPER_MIN_SHOTS = 1;
export const MAX_VO_WORDS_PER_SECOND = 2.3;

export const STORYBOARD_EDIT_FIELD = "video_storyboard" as const;

export const VIDEO_CUT_ORDER = [
  "hero_30s_16x9",
  "social_15s_9x16",
  "bumper_6s_9x16",
] as const;

export type VideoCutFormat = (typeof VIDEO_CUT_ORDER)[number];

export interface VideoShotSpec {
  shot_number: number;
  duration_seconds: ShotDurationSeconds;
  camera_motion: string;
  visual_action_prompt: string;
  voiceover_line: string;
  on_screen_super: string;
  audio_cue: string;
  anchor_concept_name: string;
  include_in_15s: boolean;
  include_in_6s: boolean;
}

export interface VideoStoryboard {
  title: string;
  creative_direction: string;
  music_direction: string;
  voiceover_style: string;
  shots: VideoShotSpec[];
}

export interface GeneratedVideoCut {
  cut: VideoCutFormat;
  label: string;
  gcs_uri: string;
  /** True when `gcs_uri` was explicitly `""` / `null` on the cut record (GCS upload skipped/failed). */
  gcs_uri_explicitly_empty?: boolean;
  artifact_key: string;
  duration_seconds: number;
  aspect_ratio: "16:9" | "9:16";
  backend: string;
  shot_numbers: number[];
  voiceover_transcript: string;
  qa_passed: boolean;
  qa_summary: string;
}

export type GeneratedVideoEntry = GeneratedVideoCut;
export type GeneratedVideosMap = Partial<Record<VideoCutFormat, GeneratedVideoCut>>;

export interface ConceptAnchorOption {
  name: string;
  summary: string;
  gcsUri: string;
}

export interface CutDurationTotals {
  heroSeconds: number;
  heroValid: boolean;
  socialSeconds: number;
  socialShotCount: number;
  socialValid: boolean;
  bumperSeconds: number;
  bumperShotCount: number;
  bumperValid: boolean;
}

/** Field errors keyed by dotted storyboard path (`title`, `shots`, `shots.0.voiceover_line`, etc.). */
export type StoryboardErrors = Record<string, string>;

/** Checkpoint-4 resume edit carrying the full `video_storyboard`. */
export type StoryboardEdit = {
  field: typeof STORYBOARD_EDIT_FIELD;
  value: VideoStoryboard;
};

type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj =>
  typeof v === "object" && v !== null && !Array.isArray(v);

function parseMaybeObj(raw: unknown): Obj | null {
  let val: unknown = raw;
  if (typeof val === "string") {
    const trimmed = val.trim();
    if (!trimmed) return null;
    try {
      val = JSON.parse(trimmed);
    } catch {
      return null;
    }
  }
  return isObj(val) ? val : null;
}

const str = (v: unknown): string => (typeof v === "string" ? v.trim() : "");

function isValidShotDuration(v: unknown): v is ShotDurationSeconds {
  return v === 4 || v === 6 || v === 8;
}

function coerceShotDuration(v: unknown): ShotDurationSeconds {
  const num = typeof v === "string" && v.trim() !== "" ? Number(v) : v;
  if (isValidShotDuration(num)) return num;
  return 6;
}

/**
 * Normalize `video_production_enabled` from session state, mirroring Python
 * `creative_agent.config.is_video_production_enabled`.
 */
export function isVideoProductionEnabled(raw: unknown): boolean {
  if (typeof raw === "boolean") return raw;
  if (typeof raw === "number") return Number.isFinite(raw) && raw !== 0;
  if (typeof raw === "string") {
    return ["1", "true", "yes", "on"].includes(raw.trim().toLowerCase());
  }
  return false;
}

/** Maximum spoken words permitted for a shot at <= 2.3 words/second (`ceil(2.3 * duration)`). */
export function maxVoWordsForDuration(durationSeconds: number): number {
  return Math.ceil(
    MAX_VO_WORDS_PER_SECOND * Math.max(1, Math.trunc(durationSeconds))
  );
}

/** Count whitespace-delimited words in a voiceover line. */
export function countWords(text: string): number {
  const trimmed = text.trim();
  return trimmed ? trimmed.split(/\s+/).filter(Boolean).length : 0;
}

/** Parse `video_storyboard` from session state; returns `null` when absent or unusable. */
export function parseVideoStoryboard(raw: unknown): VideoStoryboard | null {
  const parsed = parseMaybeObj(raw);
  if (!parsed || !Array.isArray(parsed.shots) || parsed.shots.length === 0) {
    return null;
  }

  const shots: VideoShotSpec[] = [];
  parsed.shots.forEach((item, idx) => {
    const s = parseMaybeObj(item);
    if (!s) return;
    const rawNum = s.shot_number;
    const shotNumber =
      typeof rawNum === "number" && Number.isInteger(rawNum) && rawNum >= 1
        ? rawNum
        : idx + 1;
    shots.push({
      shot_number: shotNumber,
      duration_seconds: coerceShotDuration(s.duration_seconds),
      camera_motion: str(s.camera_motion),
      visual_action_prompt: str(s.visual_action_prompt),
      voiceover_line: str(s.voiceover_line),
      on_screen_super: str(s.on_screen_super),
      audio_cue: str(s.audio_cue),
      anchor_concept_name: str(s.anchor_concept_name),
      include_in_15s: Boolean(s.include_in_15s),
      include_in_6s: Boolean(s.include_in_6s),
    });
  });

  if (shots.length === 0) return null;

  return {
    title: str(parsed.title),
    creative_direction: str(parsed.creative_direction),
    music_direction: str(parsed.music_direction),
    voiceover_style: str(parsed.voiceover_style),
    shots,
  };
}

/** Extract finalized Phase 2 visual concepts + optional rendered keyframe GCS URIs. */
export function extractConceptOptions(
  state?: Record<string, unknown>
): ConceptAnchorOption[] {
  if (!state) return [];
  const wrapper = parseMaybeObj(state.final_visual_concepts);
  const rawList = wrapper?.visual_concepts;
  if (!Array.isArray(rawList)) return [];

  const imagesMap = parseMaybeObj(state.generated_images) ?? {};
  const seen = new Set<string>();
  const out: ConceptAnchorOption[] = [];

  for (const item of rawList) {
    const obj = parseMaybeObj(item);
    if (!obj) continue;
    const name = str(obj.concept_name);
    if (!name) continue;
    const lower = name.toLowerCase();
    if (seen.has(lower)) continue;
    seen.add(lower);

    let gcsUri = "";
    const imgEntry = imagesMap[name];
    if (typeof imgEntry === "string") {
      gcsUri = imgEntry.trim();
    } else if (isObj(imgEntry)) {
      gcsUri = str(imgEntry.gcs_uri);
    }

    out.push({
      name,
      summary: str(obj.concept_summary),
      gcsUri,
    });
  }

  return out;
}

/** Collect approved copy/brand/product strings for `<datalist>` super suggestions. */
export function extractApprovedSupers(
  state?: Record<string, unknown>
): string[] {
  if (!state) return [];
  const approved: string[] = [];
  const seen = new Set<string>();

  const add = (value: unknown) => {
    const text = str(value);
    if (!text) return;
    const key = text.toLowerCase().replace(/\s+/g, " ");
    if (!seen.has(key)) {
      seen.add(key);
      approved.push(text);
    }
  };

  add(state.brand);
  add(state.target_product);

  const copyWrapper = parseMaybeObj(state.ad_copy_critique);
  if (Array.isArray(copyWrapper?.ad_copies)) {
    for (const rawCopy of copyWrapper.ad_copies) {
      const c = parseMaybeObj(rawCopy);
      if (!c) continue;
      add(c.headline);
      add(c.call_to_action);
    }
  }

  const conceptWrapper = parseMaybeObj(state.final_visual_concepts);
  if (Array.isArray(conceptWrapper?.visual_concepts)) {
    for (const rawConcept of conceptWrapper.visual_concepts) {
      const c = parseMaybeObj(rawConcept);
      if (!c) continue;
      add(c.headline);
      add(c.call_to_action);
    }
  }

  return approved;
}

/**
 * Valid 5-shot (6+8+4+6+6 = 30s) fallback storyboard used when Checkpoint 4 opens
 * after a fail-soft storyboard step so the review panel is never blank.
 */
export function fallbackVideoStoryboard(
  state?: Record<string, unknown>
): VideoStoryboard {
  const concepts = extractConceptOptions(state);
  const pickAnchor = (index: number) =>
    concepts.length > 0
      ? concepts[index % concepts.length].name
      : "Concept 1";
  const brand = str(state?.brand);
  const product = str(state?.target_product);
  const titleBase = [brand, product].filter(Boolean).join(" — ");

  const durations: ShotDurationSeconds[] = [6, 8, 4, 6, 6];
  const shots: VideoShotSpec[] = durations.map((dur, idx) => ({
    shot_number: idx + 1,
    duration_seconds: dur,
    camera_motion:
      idx === 0
        ? "Slow push-in dolly shot with shallow depth of field"
        : "Smooth tracking camera maintaining hero product framing",
    visual_action_prompt: `Commercial shot ${idx + 1} anchored on ${pickAnchor(idx)} preserving lighting and product detail.`,
    voiceover_line: "",
    on_screen_super: idx === durations.length - 1 ? brand || product : "",
    audio_cue: "Warm instrumental pulse",
    anchor_concept_name: pickAnchor(idx),
    // Shots 1 (6s), 3 (4s), 5 (6s) => 16s across 3 shots (valid 14..16s social cut).
    include_in_15s: idx === 0 || idx === 2 || idx === 4,
    // Shot 1 (6s) => 6s bumper cut.
    include_in_6s: idx === 0,
  }));

  return {
    title: titleBase ? `${titleBase} Spot` : "Campaign Video Spot",
    creative_direction:
      "Crisp 30-second commercial arc moving from cultural hook to hero product payoff.",
    music_direction: "Upbeat modern instrumental bed, strictly no vocals.",
    voiceover_style: "Warm, confident commercial delivery.",
    shots,
  };
}

/** Compute live duration sums and validity flags for the 30s, 15s, and 6s cuts. */
export function computeCutTotals(
  shots: readonly VideoShotSpec[]
): CutDurationTotals {
  let heroSeconds = 0;
  let socialSeconds = 0;
  let socialShotCount = 0;
  let bumperSeconds = 0;
  let bumperShotCount = 0;

  for (const shot of shots) {
    const dur = isValidShotDuration(shot.duration_seconds)
      ? shot.duration_seconds
      : 0;
    heroSeconds += dur;
    if (shot.include_in_15s) {
      socialSeconds += dur;
      socialShotCount += 1;
    }
    if (shot.include_in_6s) {
      bumperSeconds += dur;
      bumperShotCount += 1;
    }
  }

  return {
    heroSeconds,
    heroValid: heroSeconds === HERO_DURATION_SECONDS,
    socialSeconds,
    socialShotCount,
    socialValid:
      socialShotCount >= SOCIAL_MIN_SHOTS &&
      socialSeconds >= SOCIAL_MIN_SECONDS &&
      socialSeconds <= SOCIAL_MAX_SECONDS,
    bumperSeconds,
    bumperShotCount,
    bumperValid:
      bumperShotCount >= BUMPER_MIN_SHOTS &&
      bumperSeconds === BUMPER_DURATION_SECONDS,
  };
}

/** Create a new blank shot initialized to the first available concept anchor. */
export function createEmptyShot(
  shots: readonly VideoShotSpec[],
  state?: Record<string, unknown>
): VideoShotSpec {
  const concepts = extractConceptOptions(state);
  const fallbackAnchor =
    concepts[0]?.name || shots[shots.length - 1]?.anchor_concept_name || "";
  return {
    shot_number: shots.length + 1,
    duration_seconds: 4,
    camera_motion: "Smooth tracking shot",
    visual_action_prompt: "",
    voiceover_line: "",
    on_screen_super: "",
    audio_cue: "",
    anchor_concept_name: fallbackAnchor,
    include_in_15s: false,
    include_in_6s: false,
  };
}

/**
 * Client-side storyboard validation matching backend `validate_storyboard_edit`
 * and `storyboard_issues` timing, shot-count, anchor, and VO word-rate rules.
 */
export function validateStoryboardDraft(
  draft: VideoStoryboard,
  state?: Record<string, unknown>
): StoryboardErrors {
  const errors: StoryboardErrors = {};

  if (!draft.title.trim()) {
    errors.title = "Add a spot title.";
  }

  if (draft.shots.length < MIN_STORYBOARD_SHOTS) {
    errors.shots = `Keep at least ${MIN_STORYBOARD_SHOTS} shots.`;
  } else if (draft.shots.length > MAX_STORYBOARD_SHOTS) {
    errors.shots = `Use at most ${MAX_STORYBOARD_SHOTS} shots.`;
  }

  const concepts = extractConceptOptions(state);
  const validConceptsLower = new Set(
    concepts.map((c) => c.name.toLowerCase())
  );

  draft.shots.forEach((shot, i) => {
    if (!isValidShotDuration(shot.duration_seconds)) {
      errors[`shots.${i}.duration_seconds`] =
        "Duration must be 4s, 6s, or 8s.";
    }
    if (!shot.camera_motion.trim()) {
      errors[`shots.${i}.camera_motion`] = "Add camera motion.";
    }
    if (!shot.visual_action_prompt.trim()) {
      errors[`shots.${i}.visual_action_prompt`] =
        "Describe the visual action for this shot.";
    }
    const anchor = shot.anchor_concept_name.trim();
    if (!anchor) {
      errors[`shots.${i}.anchor_concept_name`] = "Choose an anchor concept.";
    } else if (
      validConceptsLower.size > 0 &&
      !validConceptsLower.has(anchor.toLowerCase())
    ) {
      errors[`shots.${i}.anchor_concept_name`] =
        "Anchor must match one of the finalized visual concepts.";
    }

    const words = countWords(shot.voiceover_line);
    const maxWords = maxVoWordsForDuration(shot.duration_seconds);
    if (words > maxWords) {
      errors[`shots.${i}.voiceover_line`] =
        `Voiceover has ${words} words (max ${maxWords} words for a ${shot.duration_seconds}s shot).`;
    }
  });

  const totals = computeCutTotals(draft.shots);
  if (!totals.heroValid) {
    errors.hero_duration = `30s Hero cut totals ${totals.heroSeconds}s (must equal ${HERO_DURATION_SECONDS}s).`;
  }
  if (!totals.socialValid) {
    if (totals.socialShotCount < SOCIAL_MIN_SHOTS) {
      errors.social_duration = `15s Social cut selects ${totals.socialShotCount} shot${totals.socialShotCount === 1 ? "" : "s"} (select at least ${SOCIAL_MIN_SHOTS} shots totaling ${SOCIAL_MIN_SECONDS}–${SOCIAL_MAX_SECONDS}s).`;
    } else {
      errors.social_duration = `15s Social cut totals ${totals.socialSeconds}s (must be ${SOCIAL_MIN_SECONDS}–${SOCIAL_MAX_SECONDS}s).`;
    }
  }
  if (!totals.bumperValid) {
    if (totals.bumperShotCount < BUMPER_MIN_SHOTS) {
      errors.bumper_duration = `6s Bumper cut must select at least ${BUMPER_MIN_SHOTS} shot totaling ${BUMPER_DURATION_SECONDS}s.`;
    } else {
      errors.bumper_duration = `6s Bumper cut totals ${totals.bumperSeconds}s (must equal ${BUMPER_DURATION_SECONDS}s).`;
    }
  }

  return errors;
}

/** Normalize and renumber shots 1..N for the backend `VideoStoryboard` payload. */
export function toStoryboardPayload(draft: VideoStoryboard): VideoStoryboard {
  return {
    title: draft.title.trim(),
    creative_direction: draft.creative_direction.trim(),
    music_direction: draft.music_direction.trim(),
    voiceover_style: draft.voiceover_style.trim(),
    shots: draft.shots.map((shot, idx) => ({
      shot_number: idx + 1,
      duration_seconds: shot.duration_seconds,
      camera_motion: shot.camera_motion.trim(),
      visual_action_prompt: shot.visual_action_prompt.trim(),
      voiceover_line: shot.voiceover_line.trim(),
      on_screen_super: shot.on_screen_super.trim(),
      audio_cue: shot.audio_cue.trim(),
      anchor_concept_name: shot.anchor_concept_name.trim(),
      include_in_15s: Boolean(shot.include_in_15s),
      include_in_6s: Boolean(shot.include_in_6s),
    })),
  };
}

/**
 * Build the checkpoint-4 `edits` payload when the storyboard differs from
 * `initial` (or when `initial` was absent and seeded from fallback), else `null`.
 */
export function buildStoryboardEdit(
  initial: VideoStoryboard | null,
  draft: VideoStoryboard
): StoryboardEdit[] | null {
  const value = toStoryboardPayload(draft);
  if (
    initial &&
    JSON.stringify(value) === JSON.stringify(toStoryboardPayload(initial))
  ) {
    return null;
  }
  return [{ field: STORYBOARD_EDIT_FIELD, value }];
}

const CUT_DEFAULTS: Record<
  VideoCutFormat,
  { label: string; duration_seconds: number; aspect_ratio: "16:9" | "9:16" }
> = {
  hero_30s_16x9: {
    label: "30s Hero (16:9)",
    duration_seconds: 30,
    aspect_ratio: "16:9",
  },
  social_15s_9x16: {
    label: "15s Social (9:16)",
    duration_seconds: 15,
    aspect_ratio: "9:16",
  },
  bumper_6s_9x16: {
    label: "6s Bumper (9:16)",
    duration_seconds: 6,
    aspect_ratio: "9:16",
  },
};

function isVideoCutFormat(v: string): v is VideoCutFormat {
  return (VIDEO_CUT_ORDER as readonly string[]).includes(v);
}

/** Parse `state.generated_videos` in canonical 30s -> 15s -> 6s cut order. */
export function parseGeneratedVideos(raw: unknown): GeneratedVideoCut[] {
  let val: unknown = raw;
  if (typeof val === "string") {
    const trimmed = val.trim();
    if (!trimmed) return [];
    try {
      val = JSON.parse(trimmed);
    } catch {
      return [];
    }
  }
  if (!val || typeof val !== "object") return [];

  const recordsByCut = new Map<VideoCutFormat, Obj>();
  if (Array.isArray(val)) {
    for (const item of val) {
      if (!isObj(item)) continue;
      const cutKey = str(item.cut);
      if (isVideoCutFormat(cutKey)) {
        recordsByCut.set(cutKey, item);
      }
    }
  } else {
    for (const [key, entry] of Object.entries(val as Obj)) {
      if (!isVideoCutFormat(key)) continue;
      if (typeof entry === "string" && entry.trim()) {
        recordsByCut.set(key, { gcs_uri: entry.trim() });
      } else if (isObj(entry)) {
        recordsByCut.set(key, entry);
      }
    }
  }

  const out: GeneratedVideoCut[] = [];
  for (const cut of VIDEO_CUT_ORDER) {
    const rec = recordsByCut.get(cut);
    if (!rec) continue;
    const defaults = CUT_DEFAULTS[cut];
    const rawAspect = str(rec.aspect_ratio);
    const aspectRatio: "16:9" | "9:16" =
      rawAspect === "16:9" || rawAspect === "9:16"
        ? rawAspect
        : defaults.aspect_ratio;
    const rawDur =
      typeof rec.duration_seconds === "number"
        ? rec.duration_seconds
        : Number(rec.duration_seconds);
    const durationSeconds =
      Number.isFinite(rawDur) && rawDur > 0
        ? Math.round(rawDur)
        : defaults.duration_seconds;
    const rawShots = Array.isArray(rec.shot_numbers)
      ? rec.shot_numbers
      : Array.isArray(rec.shots)
        ? rec.shots
        : [];
    const shotNumbers = rawShots
      .map((n) => (typeof n === "number" ? n : Number(n)))
      .filter((n) => Number.isInteger(n) && n >= 1);

    const gcsUri = str(rec.gcs_uri);
    const hasGcsUriProp =
      Object.prototype.hasOwnProperty.call(rec, "gcs_uri") &&
      rec.gcs_uri !== undefined;

    out.push({
      cut,
      label: str(rec.label) || defaults.label,
      gcs_uri: gcsUri,
      gcs_uri_explicitly_empty: hasGcsUriProp && gcsUri === "",
      artifact_key: str(rec.artifact_key) || `video_${cut}.mp4`,
      duration_seconds: durationSeconds,
      aspect_ratio: aspectRatio,
      backend: str(rec.backend),
      shot_numbers: shotNumbers,
      voiceover_transcript: str(rec.voiceover_transcript),
      qa_passed: rec.qa_passed !== false,
      qa_summary: str(rec.qa_summary),
    });
  }

  return out;
}

/** Human-friendly renderer label for video badges (`omniflash` -> `Omni Flash 1.1`, `veo` -> `Veo 3.1`). */
export function formatVideoBackendLabel(backend: string): string {
  const norm = backend.trim().toLowerCase();
  if (!norm) return "";
  if (norm === "omniflash" || norm.includes("omni")) return "Omni Flash 1.1";
  if (norm === "veo" || norm.startsWith("veo-")) return "Veo 3.1";
  return backend.trim();
}

/** Extract non-empty issue strings from a `<key>__issues` state entry. */
export function parseIssueList(raw: unknown): string[] {
  if (!raw) return [];
  let val: unknown = raw;
  if (typeof val === "string") {
    const trimmed = val.trim();
    if (!trimmed) return [];
    if (trimmed.startsWith("[") && trimmed.endsWith("]")) {
      try {
        val = JSON.parse(trimmed);
      } catch {
        return [trimmed];
      }
    } else {
      return [trimmed];
    }
  }
  if (Array.isArray(val)) {
    return val
      .filter((x) => x != null && String(x).trim() !== "")
      .map((x) => String(x).trim());
  }
  return [];
}

/** True when Phase 3 video attempted execution and recorded failure/exhaustion markers. */
export function hasVideoFailureMarkers(
  state: Record<string, unknown>
): boolean {
  return (
    parseIssueList(state.generated_videos__issues).length > 0 ||
    parseIssueList(state.video_storyboard__issues).length > 0 ||
    Boolean(state.video_storyboard__retry_exhausted) ||
    Boolean(state.generated_videos__retry_exhausted) ||
    Boolean(state._videos_generated__retry_exhausted)
  );
}
