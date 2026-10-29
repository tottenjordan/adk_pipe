"""Deterministic storyboard validation and multimodal video shot QA for Phase 3."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from agent_common.genai_retry import (
    build_genai_http_retry,
    model_request_timeout_ms,
)
from agent_common.locations import MODEL_LOCATION
from agent_common.sanitize import scrub_lone_surrogates

from .config import config
from .copy_gate import _avoid_terms as _brief_avoid_terms
from .copy_gate import contains_phrase
from .schemas import VideoStoryboard

logger = logging.getLogger(__name__)

VALID_SHOT_DURATIONS = (4, 6, 8)
HERO_DURATION_SECONDS = 30
SOCIAL_MIN_SECONDS = 14
SOCIAL_MAX_SECONDS = 16
SOCIAL_MIN_SHOTS = 2
BUMPER_DURATION_SECONDS = 6
BUMPER_MIN_SHOTS = 1
MAX_VO_WORDS_PER_SECOND = 2.3

_SUPER_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:—|–|\||·|•|/|:)\s*")
_AVOID_SPLIT_RE = re.compile(r"[,;\n]+")

_qa_client: genai.Client | None = None


def _get_qa_client() -> genai.Client:
    """Lazy GenAI client for multimodal video shot QA."""
    global _qa_client
    if _qa_client is None:
        _qa_client = genai.Client(
            vertexai=True,
            project=config.PROJECT_ID,
            location=MODEL_LOCATION,
            http_options=types.HttpOptions(
                retry_options=build_genai_http_retry(),
                timeout=model_request_timeout_ms(config.worker_model),
            ),
        )
    return _qa_client


def parse_storyboard(raw: Any) -> dict[str, Any] | None:
    """Normalize a ``VideoStoryboard`` instance, dict, or JSON string into a dict.

    Returns ``None`` if ``raw`` is empty or cannot be parsed as a JSON object.
    """
    if raw is None:
        return None
    if isinstance(raw, VideoStoryboard):
        return raw.model_dump()
    if isinstance(raw, BaseModel):
        dumped = raw.model_dump()
        return dumped if isinstance(dumped, dict) else None
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            return None
        return dict(loaded) if isinstance(loaded, Mapping) else None
    return None


def _as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, BaseModel):
        dumped = raw.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return dict(loaded) if isinstance(loaded, Mapping) else {}
    return {}


def extract_visual_concepts(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return finalized Phase 2 visual concepts from session state as plain dicts."""
    wrapper = _as_dict(state.get("final_visual_concepts"))
    items = wrapper.get("visual_concepts")
    if not isinstance(items, list):
        return []
    return [_as_dict(item) for item in items if _as_dict(item)]


def extract_approved_supers(state: Mapping[str, Any]) -> list[str]:
    """Collect approved copy strings (`headline`, `call_to_action`, `brand`, `target_product`)."""
    approved: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        key = " ".join(text.lower().split())
        if text and key not in seen:
            seen.add(key)
            approved.append(text)

    _add(state.get("brand"))
    _add(state.get("target_product"))

    copy_wrapper = _as_dict(state.get("ad_copy_critique"))
    copies = copy_wrapper.get("ad_copies")
    if isinstance(copies, list):
        for raw_copy in copies:
            copy_dict = _as_dict(raw_copy)
            _add(copy_dict.get("headline"))
            _add(copy_dict.get("call_to_action"))

    for concept in extract_visual_concepts(state):
        _add(concept.get("headline"))
        _add(concept.get("call_to_action"))

    return approved


def extract_avoid_terms(state: Mapping[str, Any]) -> list[str]:
    """Collect avoid terms from `creative_brief` and `visual_avoid`."""
    brief = _as_dict(state.get("creative_brief"))
    brand_cues = _as_dict(brief.get("brand"))
    raw_avoid_entries: list[str] = []
    for val in (
        brief.get("avoid"),
        brand_cues.get("do_not"),
        brief.get("brand_donts"),
    ):
        if isinstance(val, str) and val.strip():
            raw_avoid_entries.extend(
                chunk.strip() for chunk in _AVOID_SPLIT_RE.split(val) if chunk.strip()
            )
        elif isinstance(val, list):
            raw_avoid_entries.extend(
                str(item).strip() for item in val if str(item or "").strip()
            )
    raw_mandatories = brief.get("mandatories")
    mandatories = (
        [str(m) for m in raw_mandatories if isinstance(m, str)]
        if isinstance(raw_mandatories, list)
        else ()
    )
    terms = list(
        _brief_avoid_terms(
            raw_avoid_entries,
            product=str(state.get("target_product") or ""),
            mandatories=mandatories,
            trend=str(
                state.get("target_search_trends")
                or state.get("target_search_trend")
                or ""
            ),
        )
    )
    seen = {t.lower() for t in terms}
    raw_visual_avoid = str(state.get("visual_avoid") or "").strip()
    if raw_visual_avoid:
        for chunk in _AVOID_SPLIT_RE.split(raw_visual_avoid):
            cleaned = chunk.strip()
            if len(cleaned) >= 3 and cleaned.lower() not in seen:
                seen.add(cleaned.lower())
                terms.append(cleaned)
    return terms


def _normalize_super_token(text: str) -> str:
    cleaned = text.strip().strip("\"'“”‘’").strip()
    return " ".join(cleaned.lower().split())


def _super_matches_approved(super_text: str, approved_sources: Sequence[str]) -> bool:
    """Return True when `super_text` is covered by approved copy/brand/product strings."""
    norm = _normalize_super_token(super_text)
    if not norm:
        return True
    for source in approved_sources:
        src_norm = _normalize_super_token(source)
        if not src_norm:
            continue
        if norm == src_norm or contains_phrase(source, super_text):
            return True

    # Also allow compound lockups such as "Brand — Call To Action" when every
    # non-empty segment matches an approved source.
    segments = [
        seg.strip() for seg in _SUPER_SEGMENT_SPLIT_RE.split(super_text) if seg.strip()
    ]
    if len(segments) >= 2:
        return all(
            any(
                _normalize_super_token(seg) == _normalize_super_token(src)
                or contains_phrase(src, seg)
                for src in approved_sources
                if _normalize_super_token(src)
            )
            for seg in segments
        )
    return False


def max_vo_words_for_duration(duration_seconds: int) -> int:
    """Maximum spoken words permitted for a shot at <= 2.3 words/second."""
    return math.ceil(MAX_VO_WORDS_PER_SECOND * max(1, int(duration_seconds)))


def storyboard_issues(
    storyboard: Any,
    state: Mapping[str, Any] | None = None,
) -> list[str]:
    """Deterministic storyboard gate checks.

    Returns an empty list when ``storyboard`` passes every duration, voiceover
    word-rate, concept-anchor, on-screen super, and avoid-term constraint.
    """
    state_map: Mapping[str, Any] = state if state is not None else {}
    parsed = parse_storyboard(storyboard)
    if not parsed:
        return ["Storyboard is missing or could not be parsed."]

    raw_shots = parsed.get("shots")
    if not isinstance(raw_shots, list) or not raw_shots:
        return ["Storyboard has no shots."]

    issues: list[str] = []
    shots = [_as_dict(s) for s in raw_shots]
    if not (3 <= len(shots) <= 6):
        issues.append(
            f"Storyboard has {len(shots)} shot(s); must contain between 3 and 6 shots."
        )

    shot_numbers: list[int] = []
    hero_total = 0
    s15_shots: list[dict[str, Any]] = []
    s6_shots: list[dict[str, Any]] = []

    concepts = extract_visual_concepts(state_map)
    valid_concept_names = [
        str(c.get("concept_name") or "").strip()
        for c in concepts
        if str(c.get("concept_name") or "").strip()
    ]
    concept_lookup = {name.lower(): name for name in valid_concept_names}

    approved_supers = extract_approved_supers(state_map)
    avoid_terms = extract_avoid_terms(state_map)

    for idx, shot in enumerate(shots, start=1):
        raw_num = shot.get("shot_number")
        shot_num = (
            raw_num
            if isinstance(raw_num, int) and not isinstance(raw_num, bool)
            else idx
        )
        shot_numbers.append(
            shot_num
            if isinstance(raw_num, int) and not isinstance(raw_num, bool)
            else -1
        )

        raw_dur = shot.get("duration_seconds")
        if (
            isinstance(raw_dur, int)
            and not isinstance(raw_dur, bool)
            and raw_dur in VALID_SHOT_DURATIONS
        ):
            dur = raw_dur
        else:
            issues.append(
                f"Shot {shot_num}: duration_seconds {raw_dur!r} must be one of {VALID_SHOT_DURATIONS}."
            )
            dur = 0

        hero_total += dur
        if bool(shot.get("include_in_15s")):
            s15_shots.append(shot)
        if bool(shot.get("include_in_6s")):
            s6_shots.append(shot)

        if not str(shot.get("camera_motion") or "").strip():
            issues.append(f"Shot {shot_num}: camera_motion is empty.")
        if not str(shot.get("visual_action_prompt") or "").strip():
            issues.append(f"Shot {shot_num}: visual_action_prompt is empty.")

        vo_line = str(shot.get("voiceover_line") or "").strip()
        if vo_line and dur > 0:
            word_count = len([w for w in vo_line.split() if w])
            limit = max_vo_words_for_duration(dur)
            if word_count > limit:
                issues.append(
                    f"Shot {shot_num}: voiceover_line has {word_count} words "
                    f"(max {limit} words for a {dur}s shot at <= {MAX_VO_WORDS_PER_SECOND} words/s)."
                )

        anchor = str(shot.get("anchor_concept_name") or "").strip()
        if not anchor:
            issues.append(f"Shot {shot_num}: anchor_concept_name is empty.")
        elif concept_lookup and anchor.lower() not in concept_lookup:
            issues.append(
                f"Shot {shot_num}: anchor_concept_name {anchor!r} does not match "
                f"any finalized visual concept ({', '.join(valid_concept_names)})."
            )

        super_text = str(shot.get("on_screen_super") or "").strip()
        if (
            super_text
            and approved_supers
            and not _super_matches_approved(super_text, approved_supers)
        ):
            issues.append(
                f"Shot {shot_num}: on_screen_super {super_text!r} does not match "
                "any approved ad copy headline, call_to_action, brand, or target_product."
            )

        for field_name, field_val in (
            ("visual_action_prompt", str(shot.get("visual_action_prompt") or "")),
            ("voiceover_line", vo_line),
            ("on_screen_super", super_text),
        ):
            if not field_val:
                continue
            for term in avoid_terms:
                if contains_phrase(field_val, term):
                    issues.append(
                        f"Shot {shot_num}: {field_name} contains avoided term {term!r}."
                    )

    if shot_numbers != list(range(1, len(shots) + 1)):
        issues.append(
            f"Shot numbers {shot_numbers} must be sequential 1..{len(shots)}."
        )

    if hero_total != HERO_DURATION_SECONDS:
        issues.append(
            f"Hero 30s cut duration sum is {hero_total}s (must equal {HERO_DURATION_SECONDS}s)."
        )

    if len(s15_shots) < SOCIAL_MIN_SHOTS:
        issues.append(
            f"Social 15s cut selects {len(s15_shots)} shot(s) "
            f"(must select at least {SOCIAL_MIN_SHOTS} shots with include_in_15s=true)."
        )
    s15_total = sum(
        dur_val
        for s in s15_shots
        if isinstance(dur_val := s.get("duration_seconds"), int)
        and not isinstance(dur_val, bool)
    )
    if not (SOCIAL_MIN_SECONDS <= s15_total <= SOCIAL_MAX_SECONDS):
        issues.append(
            f"Social 15s cut duration sum is {s15_total}s "
            f"(must be between {SOCIAL_MIN_SECONDS}s and {SOCIAL_MAX_SECONDS}s)."
        )

    if len(s6_shots) < BUMPER_MIN_SHOTS:
        issues.append(
            "Bumper 6s cut selects 0 shots "
            "(must select at least 1 shot with include_in_6s=true)."
        )
    s6_total = sum(
        dur_val
        for s in s6_shots
        if isinstance(dur_val := s.get("duration_seconds"), int)
        and not isinstance(dur_val, bool)
    )
    if s6_total != BUMPER_DURATION_SECONDS:
        issues.append(
            f"Bumper 6s cut duration sum is {s6_total}s "
            f"(must equal {BUMPER_DURATION_SECONDS}s)."
        )

    return issues


# ---------------------------------------------------------------------------
# Multimodal Video Shot QA (gemini-3.8-flash)
# ---------------------------------------------------------------------------


class VideoQAVerdict(BaseModel):
    """Structured multimodal QA verdict for a single rendered commercial shot."""

    product_consistent: bool = Field(
        description="True when the featured product or brand identity stays recognizable and faithful to the brief without morphing or disappearing.",
    )
    text_legible: bool = Field(
        description="True when any visible on-screen text is legible and free of garbled/misspelled characters (or True when no on-screen text appears and none was required).",
    )
    motion_coherent: bool = Field(
        description="True when subject motion, physics, hands/anatomy, and camera movement look natural without severe temporal warping or flicker.",
    )
    avoid_respected: bool = Field(
        description="True when none of the campaign's avoided items or brand-safety violations appear in the video clip.",
    )
    defect_instruction: str = Field(
        default="",
        description="When any check is False, one concise imperative sentence describing ONLY the isolated visual fix needed ('' when all checks pass).",
    )


def qa_score(verdict: VideoQAVerdict) -> int:
    """Count passed binary checks (0..4)."""
    return (
        int(bool(verdict.product_consistent))
        + int(bool(verdict.text_legible))
        + int(bool(verdict.motion_coherent))
        + int(bool(verdict.avoid_respected))
    )


def qa_passed(verdict: VideoQAVerdict) -> bool:
    """Return True when all four multimodal QA checks pass."""
    return qa_score(verdict) == 4


def failed_checks(verdict: VideoQAVerdict) -> list[str]:
    """Return the names of failed checks in deterministic order."""
    failed: list[str] = []
    if not verdict.product_consistent:
        failed.append("product_consistent")
    if not verdict.text_legible:
        failed.append("text_legible")
    if not verdict.motion_coherent:
        failed.append("motion_coherent")
    if not verdict.avoid_respected:
        failed.append("avoid_respected")
    return failed


def _clean_defect_instruction(verdict: VideoQAVerdict) -> str:
    cleaned = " ".join(
        scrub_lone_surrogates(verdict.defect_instruction or "").split()
    ).strip()
    if cleaned:
        return cleaned.rstrip(".")
    checks = failed_checks(verdict)
    if not checks:
        return "Maintain visual stability and product consistency"
    return f"Resolve {', '.join(checks)} while preserving the shot composition"


def build_omniflash_retake_prompt(
    verdict: VideoQAVerdict,
    shot: Mapping[str, Any] | None = None,
) -> str:
    """Build a stateful conversational diff directive for Omni Flash 1.1 retakes."""
    del shot
    fix_clause = _clean_defect_instruction(verdict)
    return (
        "### CONVERSATIONAL EDIT DIRECTIVE\n"
        "[PRESERVATION LOCK]: Freeze original video baseline including subject identity, "
        "product appearance, environment, lighting, and camera framing.\n"
        f"[ISOLATED DIFF]: Fix ONLY: {fix_clause}. Keep everything else the same."
    )


def build_veo_repair_prompt(base_prompt: str, verdict: VideoQAVerdict) -> str:
    """Append an isolated repair directive for a stateless Veo 3.1 retake."""
    fix_clause = _clean_defect_instruction(verdict)
    return (
        f"{base_prompt.rstrip()}\n\n"
        f"QUALITY REPAIR: Fix ONLY: {fix_clause}. "
        "Keep subject identity, product appearance, lighting, and camera framing unchanged."
    )


def inspect_video_shot(
    mp4_bytes: bytes,
    shot: Mapping[str, Any],
    *,
    brand: str = "",
    target_product: str = "",
    avoid_terms: Sequence[str] = (),
    reference_description: str = "",
    client: Any = None,
    model: str | None = None,
) -> VideoQAVerdict | None:
    """Inspect a rendered `.mp4` shot with `gemini-3.8-flash` structured vision QA.

    Fail-open: returns ``None`` if the QA model call fails or returns unparseable
    output so a transient inspection error never discards a valid rendered clip.
    """
    if not mp4_bytes:
        return None

    qa_client = client if client is not None else _get_qa_client()
    qa_model = model or config.worker_model

    shot_num = shot.get("shot_number", 1)
    camera_motion = str(shot.get("camera_motion") or "").strip()
    action_prompt = str(shot.get("visual_action_prompt") or "").strip()
    expected_super = str(shot.get("on_screen_super") or "").strip()
    avoid_line = (
        ", ".join(str(t).strip() for t in avoid_terms if str(t).strip()) or "None"
    )
    ref_line = reference_description.strip() or "None provided"

    prompt = (
        "You are a commercial video quality-assurance inspector. Watch the attached MP4 shot "
        "and evaluate four binary checks strictly and objectively:\n"
        f"- Brand: {brand or 'N/A'}\n"
        f"- Target product: {target_product or 'N/A'}\n"
        f"- Reference product details: {ref_line}\n"
        f"- Shot {shot_num} camera directive: {camera_motion}\n"
        f"- Shot {shot_num} visual action: {action_prompt}\n"
        f"- Expected on-screen super (optional): {expected_super or 'None'}\n"
        f"- Must avoid: {avoid_line}\n\n"
        "Rules:\n"
        "1. `product_consistent`: False if the product morphs, vanishes unexpectedly, or contradicts the product identity.\n"
        "2. `text_legible`: False ONLY if visible text in the video is garbled, misspelled, or unreadable. If no text appears and none is needed, mark True.\n"
        "3. `motion_coherent`: False if there is severe anatomical distortion, broken physics, or jarring temporal flicker.\n"
        "4. `avoid_respected`: False if any item in the Must avoid list appears visually.\n"
        "5. `defect_instruction`: If any check is False, write ONE short imperative sentence naming ONLY the isolated visual fix needed; if all four pass, return ''."
    )

    try:
        response = qa_client.models.generate_content(
            model=qa_model,
            contents=[
                types.Part.from_bytes(data=mp4_bytes, mime_type="video/mp4"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=VideoQAVerdict,
            ),
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, VideoQAVerdict):
            return parsed
        if isinstance(parsed, Mapping):
            return VideoQAVerdict.model_validate(dict(parsed))
        raw_text = scrub_lone_surrogates(getattr(response, "text", "") or "").strip()
        if not raw_text:
            return None
        return VideoQAVerdict.model_validate_json(raw_text)
    except Exception as exc:
        logger.warning(
            "Video QA inspection failed on shot %s (fail-open): %s", shot_num, exc
        )
        return None
