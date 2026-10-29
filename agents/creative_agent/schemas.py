"""Pydantic output schemas for the creative_agent pipeline agents."""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# --- RESEARCH FEEDBACK SCHEMA --- #
class SearchQuery(BaseModel):
    """Model representing a specific search query for web search."""

    search_query: str = Field(
        description="A highly specific and targeted query for web search."
    )


class ResearchFeedback(BaseModel):
    """Model for providing evaluation feedback on research quality."""

    finding_type: Literal["Gap", "Opportunity"] = Field(
        description="Evaluation result. 'Gap' if gathering missing data is most critical, 'Opportunity' if the remaining research should focus on exploring the nuances of the overlap/sentiment."
    )

    analysis_comment: str = Field(
        description="Detailed explanation of the gap found OR the opportunity identified (max 3 sentences)."
    )

    follow_up_queries: list[SearchQuery] | None = Field(
        default=None,
        description="A list of specific, targeted follow-up search queries to either fill the identified gap or explore the highest-potential opportunity",
    )


# --- AD COPY SCHEMA ---
class AdCopy(BaseModel):
    """Model representing a single Ad Copy idea"""

    id: int = Field(description="Numerical identifier; use values 1-10.")
    tone_style: Literal[
        "Humorous",
        "Aspirational",
        "Problem/Solution",
        "Emotional/Authentic",
        "Educational/Informative",
        "Relatable/Meme-based",
    ] = Field(description="Specify one of the required tones.")
    angle_id: str = Field(
        default="",
        description="The id of the creative brief angle this copy executes (e.g. 'A2'); '' when there is no brief.",
    )
    headline: str = Field(description="A short, attention-grabbing Headline.")
    body_text: str = Field(
        description="2-3 sentences of concise and compelling ad copy."
    )
    trend_connection: str = Field(
        description="A sentence explaining how this copy leverages or references the target search trend."
    )
    audience_appeal_rationale: str = Field(
        description="A brief, 1-sentence rationale for why this idea will appeal to the target audience, based on the research report."
    )
    social_caption: str = Field(
        description="A candidate, short social media caption (e.g., for Instagram or TikTok video description)."
    )
    typicality: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Honest self-rating 0-1 of how expected this idea is for its angle: 1 = the most obvious, expected execution; 0 = a highly unexpected one.",
    )
    uses_trend_state: bool = Field(
        default=False,
        description="True only for the timely creative that references the validated current moment (when the timely rules allow one); false for every evergreen creative.",
    )
    timely_reason: str = Field(
        default="",
        description="One sentence: how this creative references the current moment; '' when it is evergreen.",
    )


class AdCopyList(BaseModel):
    """Model for efficiently providing ad copy ideas for the critic agent to consume."""

    ad_copies: list[AdCopy] | None = Field(
        default=None,
        description="A list of 10 initial ad copy ideas.",
    )


# --- AD COPY SCHEMA FINAL ---
BriefCheckItem = Literal[
    "proposition",
    "product",
    "reason_to_believe",
    "trend_bridge",
    "tone",
    "mandatories",
    "avoid",
    "cta",
    "risks",
]


class BriefCheck(BaseModel):
    """One item of the critic's brief checklist for a final ad copy."""

    item: BriefCheckItem = Field(
        description="The checklist item: 'proposition' (delivers the single-minded proposition), 'product' (names the target product), 'reason_to_believe' (uses at least one reason to believe), 'trend_bridge' (connects to the trend through the bridge in the stated fit_mode, with a clear, specific tie that someone who follows the trend would recognise), 'tone' (matches the brand tone of voice), 'mandatories' (honours every mandatory), 'avoid' (contains nothing from the avoid list), 'cta' (specific call to action with an action verb that matches the desired response), 'risks' (respects the brief's trend risks: never jokes about a real disaster's severity, victims, damage or official warnings)."
    )
    passed: bool = Field(
        description="False only when the final copy (headline, body, caption and CTA) clearly fails this item; be accurate, not harsh."
    )
    note: str = Field(
        description="A short reason: what meets the item, or exactly what is missing or wrong."
    )


class FinalAdCopy(BaseModel):
    """Model representing a single Ad Copy idea"""

    original_id: int = Field(description="Retain the original ID for traceability.")
    tone_style: Literal[
        "Humorous",
        "Aspirational",
        "Problem/Solution",
        "Emotional/Authentic",
        "Educational/Informative",
        "Relatable/Meme-based",
    ] = Field(description="The tone/style from the original idea (e.g., Humorous).")
    angle_id: str = Field(
        default="",
        description="The creative brief angle id carried over from the original idea (e.g. 'A2'); '' when there is no brief.",
    )
    headline: str = Field(description="The finalized, attention-grabbing Headline.")
    body_text: str = Field(description="The finalized, concise and compelling ad copy.")
    trend_connection: str = Field(
        description="A sentence explaining how this copy leverages or references the target search trend."
    )
    audience_appeal_rationale: str = Field(
        description="A brief, 1-sentence rationale for target audience appeal."
    )
    social_caption: str = Field(
        description="The finalized candidate social media caption."
    )
    typicality: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="The original idea's typicality self-rating (0-1; 1 = the most expected idea for its angle, 0 = highly unexpected), re-rated if the copy changed.",
    )
    uses_trend_state: bool = Field(
        default=False,
        description="True only for the timely creative that references the validated current moment (when the timely rules allow one); false for every evergreen creative.",
    )
    timely_reason: str = Field(
        default="",
        description="One sentence: how this creative references the current moment; '' when it is evergreen.",
    )
    call_to_action: str = Field(
        description="A NEW, specific, action-oriented phrase that starts with an action verb, matches the brief's desired response and has at most 8 words (e.g., 'Shop the drop now!')."
    )
    brief_checks: list[BriefCheck] = Field(
        default_factory=list,
        description="The brief checklist for this final copy: exactly one entry per item (proposition, product, reason_to_believe, trend_bridge, tone, mandatories, avoid, cta, risks), judged accurately.",
    )
    detailed_performance_rationale: str = Field(
        description="A 2-3 sentence strategic critique explaining *why* this ad copy will perform well against the selection criteria."
    )


class FinalAdCopyList(BaseModel):
    """Model for efficiently providing ad copy ideas for the critic agent to consume."""

    ad_copies: list[FinalAdCopy] | None = Field(
        default=None,
        description="A list of the finalized ad copy ideas.",
    )


# --- VISUAL CONCEPT SCHEMA ---
CompositionMode = Literal[
    "product_hero",
    "product_in_scene",
    "cast_with_product",
    "lifestyle_context",
    "graphic_lockup",
]


class VisualConcept(BaseModel):
    """Model representing a single candidate visual concept."""

    ad_copy_id: int = Field(
        description="The paired ad copy's `original_id` (a direct link to the ad copy)."
    )
    concept_name: str = Field(
        description="A short, intuitive name for the visual concept."
    )
    trend_visual_link: str = Field(
        description="A 1-sentence description of how the visual specifically incorporates the target search trend."
    )
    concept_summary: str = Field(
        description="A 2-3 sentence explanation of the creative concept and its link to the ad copy's message."
    )
    visual_style: str = Field(
        default="",
        description="The chosen style family for this concept, written exactly as its IMAGE_PROMPT_GUIDE style palette name (e.g. '2D flat / vector cartoon', 'Candid 35mm film photo', 'Diecut sticker'), selected to fit the ad's tone/audience — NOT defaulted to photorealism.",
    )
    aspect_ratio: str = Field(
        default="",
        description="The chosen aspect ratio for this concept: '9:16' (default vertical reel), '1:1' (square feed), or '3:4' (portrait) — or the campaign-wide aspect-ratio override when one is set.",
    )
    trend_motif: str = Field(
        default="",
        description="A short concrete VISUAL element SPECIFIC to the trend (noun phrase, ≤8 words): its signature imagery, recognisable at a glance; never generic social-media imagery (phones, feeds, chat bubbles). MUST appear verbatim in image_generation_prompt.",
    )
    brand_cue: str = Field(
        default="",
        description="The brand distinctive asset this image shows (e.g. 'PRS bird inlays', 'the red ACME crate'), taken from the creative brief's brand distinctive assets or the user's brand colours, and named visibly in image_generation_prompt; '' only when no distinctive asset is known.",
    )
    angle_id: str = Field(
        default="",
        description="The creative brief angle id carried over from the paired ad copy (its `angle_id`, e.g. 'A2'); '' when the copy has none.",
    )
    composition_mode: CompositionMode = Field(
        default="product_in_scene",
        description="Composition framing mode: 'product_hero' (pure packshot/studio hero — zero humans, faces or hands), 'product_in_scene' (product grounded in a contextual scene), 'cast_with_product' (on-camera talent/person interacting with or presenting the product), 'lifestyle_context' (wider lifestyle or cultural scene featuring the product), or 'graphic_lockup' (typographic, poster or emblem graphic composition — zero human models).",
    )
    uses_trend_state: bool = Field(
        default=False,
        description="True only for the timely creative that references the validated current moment (when the timely rules allow one); false for every evergreen creative.",
    )
    timely_reason: str = Field(
        default="",
        description="One sentence: how this creative references the current moment; '' when it is evergreen.",
    )
    casts_person_reference: bool = Field(
        default=False,
        description="True when this concept's single human hero is the user's person reference (the person in the person reference image); only when person_reference_available is yes and the casting rules hold.",
    )
    person_casting_reason: str = Field(
        default="",
        description="One sentence: why this concept casts (or would not cast) the person reference; '' when no person reference is available.",
    )
    image_generation_prompt: str = Field(
        description="A draft prompt for image generation."
    )


class VisualConceptList(BaseModel):
    """Model listing all initial visual concepts."""

    visual_concepts: list[VisualConcept] | None = Field(
        default=None,
        description="A list of candidate visual concepts.",
    )


# --- VISUAL CONCEPT CRITIQUE SCHEMA ---
class VisualConceptCritique(BaseModel):
    """Model representing the critique of a single candidate visual concept."""

    ad_copy_id: int = Field(
        description="Retains the original ID for a direct link to the ad copy."
    )
    concept_name: str = Field(description="The original visual concept name.")
    trend_visual_link: str = Field(description="The original trend link description.")
    concept_summary: str = Field(
        description="The original creative concept explanation."
    )
    visual_style: str = Field(
        default="",
        description="The chosen style family for this concept, carried through from the draft (refine within it; do not force to photorealism).",
    )
    aspect_ratio: str = Field(
        default="",
        description="The chosen aspect ratio ('9:16', '1:1', or '3:4'), carried through from the draft.",
    )
    trend_motif: str = Field(
        default="",
        description="A short concrete VISUAL element SPECIFIC to the trend (noun phrase, ≤8 words): its signature imagery, recognisable at a glance; never generic social-media imagery (phones, feeds, chat bubbles). MUST appear verbatim in image_generation_prompt.",
    )
    brand_cue: str = Field(
        default="",
        description="The brand distinctive asset this image shows (e.g. 'PRS bird inlays', 'the red ACME crate'), taken from the creative brief's brand distinctive assets or the user's brand colours, and named visibly in image_generation_prompt; '' only when no distinctive asset is known.",
    )
    angle_id: str = Field(
        default="",
        description="The creative brief angle id carried over from the paired ad copy (its `angle_id`, e.g. 'A2'); '' when the copy has none.",
    )
    uses_trend_state: bool = Field(
        default=False,
        description="True only for the timely creative that references the validated current moment (when the timely rules allow one); false for every evergreen creative.",
    )
    timely_reason: str = Field(
        default="",
        description="One sentence: how this creative references the current moment; '' when it is evergreen.",
    )
    casts_person_reference: bool = Field(
        default=False,
        description="True when this concept's single human hero is the user's person reference (the person in the person reference image); only when person_reference_available is yes and the casting rules hold.",
    )
    person_casting_reason: str = Field(
        default="",
        description="One sentence: why this concept casts (or would not cast) the person reference; '' when no person reference is available.",
    )
    composition_mode: CompositionMode = Field(
        default="product_in_scene",
        description="Composition framing mode carried through or refined from the draft ('product_hero', 'product_in_scene', 'cast_with_product', 'lifestyle_context', or 'graphic_lockup').",
    )
    image_generation_prompt: str = Field(
        description="The FINAL, refined image-generation prompt, written in the concept's chosen visual_style and at a length appropriate to that style."
    )
    critique_summary: str = Field(
        description="A brief (1-2 sentence) summary of the key changes made to the prompt."
    )


class VisualConceptCritiqueList(BaseModel):
    """Model listing all initial visual concepts."""

    visual_concepts: list[VisualConceptCritique] | None = Field(
        default=None,
        description="A list of visual concept critiques.",
    )


# --- VISUAL CONCEPT FINAL SCHEMA ---
class VisualConceptFinal(BaseModel):
    """Model representing a finalized visual concept."""

    ad_copy_id: int = Field(
        description="Retains the original ID for a direct link to the ad copy."
    )
    concept_name: str = Field(description="The finalized name of the visual concept.")
    trend: str = Field(description="The trend referenced by this visual concept.")
    trend_reference: str = Field(
        description="How the visual concept relates to the target search trend"
    )
    markets_product: str = Field(
        description="A brief explanation of how this markets the target product"
    )
    audience_appeal: str = Field(
        description="A brief explanation for the target audience appeal."
    )
    selection_rationale: str = Field(
        description="A brief rationale explaining why this final visual concept will perform well"
    )
    headline: str = Field(
        description="The final Headline text from the original ad copy."
    )
    social_caption: str = Field(
        description="The final social media caption from the original ad copy."
    )
    call_to_action: str = Field(
        description="The final Call-to-Action from the original ad copy."
    )
    concept_summary: str = Field(
        description="A final, brief (2-3 sentence) summary of the combined ad copy and visual concept."
    )
    visual_style: str = Field(
        default="",
        description="The final chosen style family for this concept (carried through from the critique).",
    )
    aspect_ratio: str = Field(
        default="",
        description="The final chosen aspect ratio ('9:16', '1:1', or '3:4') for this concept.",
    )
    trend_motif: str = Field(
        default="",
        description="A short concrete VISUAL element SPECIFIC to the trend (noun phrase, ≤8 words): its signature imagery, recognisable at a glance; never generic social-media imagery (phones, feeds, chat bubbles). MUST appear verbatim in image_generation_prompt.",
    )
    brand_cue: str = Field(
        default="",
        description="The brand distinctive asset this image shows (e.g. 'PRS bird inlays', 'the red ACME crate'), taken from the creative brief's brand distinctive assets or the user's brand colours, and named visibly in image_generation_prompt; '' only when no distinctive asset is known.",
    )
    angle_id: str = Field(
        default="",
        description="The creative brief angle id carried over from the paired ad copy (its `angle_id`, e.g. 'A2'); '' when the copy has none.",
    )
    uses_trend_state: bool = Field(
        default=False,
        description="True only for the timely creative that references the validated current moment (when the timely rules allow one); false for every evergreen creative.",
    )
    timely_reason: str = Field(
        default="",
        description="One sentence: how this creative references the current moment; '' when it is evergreen.",
    )
    casts_person_reference: bool = Field(
        default=False,
        description="True when this concept's single human hero is the user's person reference (the person in the person reference image); only when person_reference_available is yes and the casting rules hold.",
    )
    person_casting_reason: str = Field(
        default="",
        description="One sentence: why this concept casts (or would not cast) the person reference; '' when no person reference is available.",
    )
    composition_mode: CompositionMode = Field(
        default="product_in_scene",
        description="The final composition framing mode ('product_hero', 'product_in_scene', 'cast_with_product', 'lifestyle_context', or 'graphic_lockup') for this concept.",
    )
    image_generation_prompt: str = Field(
        description="The final, revised image-generation prompt, written in the concept's chosen visual_style."
    )


class VisualConceptFinalList(BaseModel):
    """Model listing all finalized visual concepts."""

    visual_concepts: list[VisualConceptFinal] | None = Field(
        default=None,
        description="A list of finalized visual concept.",
    )


# --- CREATIVE BRIEF SCHEMA ---
# The structured, fit-tested brief every downstream creative agent treats as the
# contract (written by brief_writer after the research report). The numeric /
# list-length bounds are deliberate model-facing constraints: google-genai maps
# ge/le to Vertex Schema minimum/maximum and min_length/max_length to
# min_items/max_items (asserted in tests/test_schemas.py), and ADK's
# output_schema validation turns a violating sample into a pydantic
# ValidationError, which SCHEMA_RETRY re-draws. Rules the schema cannot express
# (one-sentence proposition, tension-based insight, cited RTBs, fit_mode
# consistency, specific motifs) live in creative_agent/brief_check.py and are
# fed back to brief_reviser.
class TrendBridge(BaseModel):
    """How (and how hard) the brand should connect to the trend."""

    fit_score: int = Field(
        ge=1,
        le=5,
        description="Brand-trend fit, 1-5: 5 = the product is naturally part of the trend; 4 = a clear product/benefit link; 3 = a shared cultural value or mood but no product link; 2 = only the trend's tone or format is borrowable; 1 = no credible link (or brand-safety risk).",
    )
    fit_mode: Literal["direct", "cultural", "light_touch"] = Field(
        description="Derived from fit_score: 'direct' (score 4-5, the product plays in the trend), 'cultural' (score 3, connect through the shared value or mood), 'light_touch' (score 1-2, borrow the trend's tone, mood or format only; never force the product into the trend)."
    )
    bridge: str = Field(
        description="One sentence naming which brand/product trait connects to which specific facet of the trend."
    )
    motifs: list[str] = Field(
        description="2-4 concrete visual or verbal motifs SPECIFIC to this trend (signature objects, colours, places, events, rituals or memes), recognisable at a glance; never generic social-media imagery (phones, feeds, chat bubbles, hashtags). Real people only via their iconography, never a likeness."
    )
    risks: list[str] = Field(
        description="Brand-safety and cultural risks of joining this trend (controversies, real-person sensitivities, negative associations)."
    )
    timely_angle: str = Field(
        default="",
        description="Only when the current moment is present and timely: one line on how a timely creative may reference it (tone per the timely playbook); otherwise ''.",
    )
    safer_angle: str = Field(
        default="",
        description="Only when the run asks for a safer angle on a sensitive trend: one line (at most 25 words) naming the respectful, positive association to build on (e.g. the subject's legacy), never the sensitive event; otherwise ''.",
    )


class ReasonToBelieve(BaseModel):
    """A single proof point supporting the proposition."""

    claim: str = Field(description="A concrete, checkable proof point.")
    source_id: str | None = Field(
        description="Supporting source id: 'src-N' (web research), 'ent-N' (enterprise knowledge base), or 'brief' (user-provided brief)."
    )


class BrandCues(BaseModel):
    """How the work must look and sound like the brand."""

    tone_of_voice: str = Field(
        description="The brand's tone of voice in a short phrase (e.g. 'warm, witty, never sarcastic')."
    )
    distinctive_assets: list[str] = Field(
        description="Brand distinctive assets to show (logo, colours, characters, packaging, sonic or verbal signatures) from the research report and the user's brand colours."
    )
    do_not: list[str] = Field(
        description="Brand voice and visual don'ts (things the brand never says or shows)."
    )


class CreativeAngle(BaseModel):
    """One distinct creative route, rooted in a different audience tension."""

    angle_id: str = Field(description="Angle identifier: 'A1' to 'A5'.")
    name: str = Field(description="A short, memorable name for the angle.")
    tension: str = Field(
        description="The audience tension this angle resolves (distinct from every other angle's tension, not a tone variant)."
    )
    route: str = Field(
        description="1-2 sentences on how the creative executes the angle and delivers the proposition."
    )


class CreativeBrief(BaseModel):
    """The structured creative brief: the contract for the ad copy and visual agents."""

    objective: str = Field(
        description="The single business/communication objective of this campaign moment."
    )
    audience: str = Field(
        description="Who we are talking to, sharpened from the target audience with research insight."
    )
    insight: str = Field(
        description="A human tension specific to THIS brand's audience, written as 'X, but Y'."
    )
    single_minded_proposition: str = Field(
        description="ONE sentence, one idea (no 'and'): the single thing we want the audience to take away."
    )
    reasons_to_believe: list[ReasonToBelieve] = Field(
        description="2-4 proof points for the proposition, each citing a source id."
    )
    brand: BrandCues = Field(description="Brand tone and distinctive assets.")
    trend_bridge: TrendBridge = Field(
        description="The brand-trend fit test and how to connect to the trend."
    )
    mandatories: list[str] = Field(
        description="Must-include elements (selling points, product name, legal or brand requirements)."
    )
    avoid: list[str] = Field(
        description="Short terms to keep out of the work: the user's avoid list and the trend risks (brand don'ts go in brand.do_not)."
    )
    desired_response: str = Field(
        description="What the audience should think, feel and do after seeing the ad."
    )
    angles: list[CreativeAngle] = Field(
        min_length=3,
        max_length=5,
        description="3-5 genuinely different creative angles, each rooted in a different audience tension.",
    )


# --- TREND SITUATION (trend-state awareness) --- #
# The trend's current moment: drafted by the grounded trend_situation branch
# (searcher -> synthesizer, output_schema=TrendSituationDraft) and turned into a
# validated TrendSituation by the deterministic validator in
# creative_agent/trend_situation.py, which abstains to phase "unknown" unless the
# event date (and outcome) are source-backed and inside the event window.
TrendPhase = Literal[
    "upcoming", "live", "just_happened", "ongoing", "evergreen", "sensitive", "unknown"
]


class TrendSituationDraft(BaseModel):
    """The synthesizer's draft of the trend's current moment (not yet validated)."""

    phase: TrendPhase = Field(
        description="'upcoming' = a dated event is coming soon; 'live' = happening right now; 'just_happened' = a dated event ended recently with a known outcome; 'ongoing' = a continuing story with no single dated event; 'evergreen' = no time-bound event; 'sensitive' = a tragedy, injury, death, scandal or disaster; 'unknown' = the sources do not state the event date (choose this when unsure)."
    )
    event_name: str = Field(
        default="",
        description="The specific event driving interest (e.g. 'Texas vs Oklahoma'), or empty.",
    )
    event_start: str = Field(
        default="",
        description="The event's start as an ISO date (YYYY-MM-DD) or datetime, exactly as the sources state it; empty when no source states it.",
    )
    event_end: str = Field(
        default="",
        description="The event's end as an ISO date or datetime when it differs from the start, else empty.",
    )
    # "none", not "": Vertex rejects an empty string in a response_schema enum.
    outcome: Literal["win", "loss", "draw", "other", "none"] = Field(
        default="none",
        description="For 'just_happened' only: the result from the side the target audience supports, or the first-named subject's side when the audience is neutral or unclear (win / loss / draw), 'other' for a non-sports outcome, else 'none'.",
    )
    outcome_summary: str = Field(
        default="",
        description="At most one factual sentence describing the outcome, as stated by the sources; empty when there is none.",
    )
    audience_sentiment: Literal["positive", "negative", "mixed", "neutral"] = Field(
        default="neutral",
        description="The prevailing public sentiment around the moment.",
    )
    source_ids: list[str] = Field(
        default_factory=list,
        description="The 'src-N' ids from the sources that state the event date and outcome; only ids present in the sources.",
    )
    confidence: Literal["high", "medium", "low"] = Field(
        default="low",
        description="How confident the sources make you in the phase, date and outcome.",
    )

    @field_validator("outcome", mode="before")
    @classmethod
    def _legacy_empty_outcome(cls, value: Any) -> Any:
        """Older drafts and code paths used "" (or null) for no outcome."""
        return "none" if value in ("", None) else value


class TrendSituation(TrendSituationDraft):
    """The validated current moment (state `trend_situation`)."""

    as_of: str = Field(
        default="", description="When it was validated (ISO datetime with offset)."
    )
    timely_ok: bool = Field(
        default=False,
        description="True only when a creative may reference the moment.",
    )
    valid_until: str = Field(
        default="",
        description="Until when a timely reference stays valid (ISO datetime), else empty.",
    )
    tone_rule: str = Field(
        default="", description="The phase-conditioned tone for a timely reference."
    )
    abstain_reason: str = Field(
        default="",
        description="Why the validator abstained (phase forced to 'unknown') or why the moment is not timely.",
    )


# --- PHASE 3 VIDEO PRODUCTION SCHEMAS (OPT-IN) ---
VideoCutFormat = Literal["hero_30s_16x9", "social_15s_9x16", "bumper_6s_9x16"]


class VideoShotSpec(BaseModel):
    """Specification for a single commercial video shot anchored on a Phase 2 keyframe."""

    shot_number: int = Field(
        ge=1,
        description="1-based sequential shot number within the storyboard (1..N).",
    )
    duration_seconds: Literal[4, 6, 8] = Field(
        description="Shot duration in seconds; must be 4, 6, or 8.",
    )
    camera_motion: str = Field(
        description="Explicit camera movement and framing directive (e.g. 'Slow push-in dolly shot, shallow depth of field').",
    )
    visual_action_prompt: str = Field(
        description="Detailed visual motion and scene action anchored on the Phase 2 keyframe, preserving product identity, composition framing, and lighting.",
    )
    voiceover_line: str = Field(
        default="",
        description="Spoken narration line for this shot (at most ceil(2.3 * duration_seconds) words; '' for silent/music-only beat).",
    )
    on_screen_super: str = Field(
        default="",
        description="Optional short on-screen text super; when non-empty, must match an approved ad copy headline/CTA, the brand name, or the target product name.",
    )
    audio_cue: str = Field(
        default="",
        description="Diegetic sound or musical transition cue for this shot (e.g. 'crisp snare hit and warm synth swell').",
    )
    anchor_concept_name: str = Field(
        description="Exact `concept_name` of the finalized Phase 2 visual concept whose rendered image anchors this shot.",
    )
    include_in_15s: bool = Field(
        default=False,
        description="True when this shot belongs to the 15-second 9:16 social cutdown (selected shots must total 14..16 seconds across at least 2 shots).",
    )
    include_in_6s: bool = Field(
        default=False,
        description="True when this shot belongs to the 6-second 9:16 bumper cutdown (selected shots must total 6 seconds across at least 1 shot).",
    )


class VideoStoryboard(BaseModel):
    """Multi-cut commercial storyboard for a 30s hero spot plus 15s social and 6s bumper cutdowns."""

    title: str = Field(
        description="Short commercial spot title connecting the brand/product and trend.",
    )
    creative_direction: str = Field(
        default="",
        description="1-2 sentences summarizing pacing, visual continuity, and commercial arc.",
    )
    music_direction: str = Field(
        default="",
        description="Instrumental music bed direction (genre, tempo, instrumentation, mood; strictly instrumental, no vocals).",
    )
    voiceover_style: str = Field(
        default="",
        description="Voiceover delivery direction matching the brand tone of voice (e.g. 'warm, confident, conversational').",
    )
    shots: list[VideoShotSpec] = Field(
        min_length=3,
        max_length=6,
        description="3-6 sequential shots whose durations sum to 30 seconds for the 16:9 hero commercial.",
    )
