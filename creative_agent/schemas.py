"""Pydantic output schemas for the creative_agent pipeline agents."""

from typing import Literal

from pydantic import BaseModel, Field


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


class AdCopyList(BaseModel):
    """Model for efficiently providing ad copy ideas for the critic agent to consume."""

    ad_copies: list[AdCopy] | None = Field(
        default=None,
        description="A list of 10 initial ad copy ideas.",
    )


# --- AD COPY SCHEMA FINAL ---
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
    call_to_action: str = Field(
        description="A NEW, catchy, action-oriented phrase (e.g., 'Shop the drop now!')."
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
class VisualConcept(BaseModel):
    """Model representing a single candidate visual concept."""

    ad_copy_id: int = Field(
        description="Retains the original ID for a direct link to the ad copy."
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
        description="The chosen style family for this concept (e.g. 'flat 2D vector cartoon', 'candid 35mm film photo', 'diecut sticker'), selected from the IMAGE_PROMPT_GUIDE style palette to fit the ad's tone/audience — NOT defaulted to photorealism.",
    )
    aspect_ratio: str = Field(
        default="",
        description="The chosen aspect ratio for this concept: '9:16' (default vertical reel), '1:1' (square feed), or '3:4' (portrait) — or the campaign-wide aspect-ratio override when one is set.",
    )
    trend_motif: str = Field(
        default="",
        description="A short concrete VISUAL element SPECIFIC to the trend (noun phrase, ≤8 words): its signature imagery, recognisable at a glance; never generic social-media imagery (phones, feeds, chat bubbles). MUST appear verbatim in image_generation_prompt.",
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


class ReasonToBelieve(BaseModel):
    """A single proof point supporting the proposition."""

    claim: str = Field(description="A concrete, checkable proof point.")
    source_id: str | None = Field(
        description="The supporting source: a 'src-N' id from the research sources, or 'brief' when the claim comes from the user's key selling points."
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
        description="Things to keep out of the work (user avoid list, trend risks, brand don'ts)."
    )
    desired_response: str = Field(
        description="What the audience should think, feel and do after seeing the ad."
    )
    angles: list[CreativeAngle] = Field(
        min_length=3,
        max_length=5,
        description="3-5 genuinely different creative angles, each rooted in a different audience tension.",
    )
