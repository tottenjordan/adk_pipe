export interface Part {
  text?: string;
  functionCall?: {
    id?: string;
    name: string;
    args: Record<string, unknown>;
  };
  functionResponse?: {
    id?: string;
    name: string;
    response: Record<string, unknown>;
  };
  inlineData?: {
    mimeType: string;
    data: string;
  };
}

export interface Content {
  role: string;
  parts: Part[];
}

export interface AgentEvent {
  id: string;
  invocationId: string;
  author: string;
  content?: Content;
  actions?: {
    stateDelta?: Record<string, unknown>;
    artifactDelta?: Record<string, string>;
  };
  longRunningToolIds?: string[];
  partial?: boolean;
  timestamp: number;
  // Present on failure events emitted by the ADK run_sse stream (e.g. a model
  // 429). `errorCode`/`errorMessage` ride on a normal event envelope; some
  // terminal failures instead arrive as a bare `{ error, error_details }`.
  errorCode?: string;
  errorMessage?: string;
  error?: string;
}

export interface Session {
  id: string;
  appName: string;
  userId: string;
  state: Record<string, unknown>;
  events: AgentEvent[];
  /** Seconds since the epoch (ADK serializes `last_update_time` as a float). */
  lastUpdateTime?: number;
}

export interface CampaignInput {
  agent: "trend_scout" | "creative_agent" | "interactive_creative";
  brand: string;
  targetAudience: string;
  targetProduct: string;
  keySellingPoints: string;
  targetSearchTrend?: string;
  /**
   * trend_scout only: when true, the run pauses after gathering the top ~25
   * trends so the user can pick which to keep (via the `review_trends`
   * LongRunningFunctionTool). Wired into the session's initial state as
   * `interactive_trend_pick`.
   */
  interactiveTrendPick?: boolean;
  /**
   * creative_agent / interactive_creative only: opt this run in to learning
   * from the brand's past human ratings (guidance, style steering, stricter
   * checks). Seeded as `learn_from_ratings: true` only when checked.
   */
  learnFromRatings?: boolean;
  /**
   * creative_agent / interactive_creative only: one of the caller's active
   * person consents (/people) that the visual agents may cast as a concept's
   * hero. Seeded as `person_reference: {uri, consent_id}` only when chosen; the
   * api re-checks the consent at kick-off.
   */
  personReference?: PersonReferenceInput | null;
  /**
   * creative_agent / interactive_creative only: an optional gs:// or http(s)
   * URL to a product/brand reference image. Threaded into the session's initial
   * state as `reference_image_uri` so image generation can apply it to every
   * concept for likeness/consistency.
   */
  referenceImageUri?: string;
  /**
   * creative_agent / interactive_creative only: optional user-supplied visual
   * intent, all threaded into the session's initial state as snake_case keys
   * (see buildInitialState) and consumed by the visual prompts / image tool.
   */
  /** Free-text art direction → `visual_intent` prompt token. */
  visualIntent?: string;
  /** Brand colour palette → `brand_colors` prompt token. */
  brandColors?: string;
  /** Preferred STYLE_PALETTE family → `visual_style_preference` (seed). */
  visualStylePreference?: string;
  /** Elements to keep out → `visual_avoid` (reframed positively). */
  visualAvoid?: string;
  /** Deterministic aspect-ratio override → `visual_aspect_ratio`. */
  visualAspectRatio?: string;
  /** How to use the reference image → `reference_image_role` (product|logo|style). */
  referenceImageRole?: string;
  /**
   * Reference rows 2–3 (row 1 is `referenceImageUri`/`referenceImageRole`).
   * All rows are seeded together as `reference_images` (see buildInitialState).
   */
  extraReferenceImages?: ReferenceRowInput[];
}

/** One reference image for image generation: a gs:// or http(s) URI + role. */
/** A consented person reference chosen for a run (`person_reference` in state). */
export interface PersonReferenceInput {
  uri: string;
  consentId: string;
}

export interface ReferenceImageInput {
  uri: string;
  /** product | logo | style ("" in the form = product). */
  role: string;
}

/** A form reference row: `id` is a client-only React key (never sent). */
export interface ReferenceRowInput extends ReferenceImageInput {
  id?: string;
}
