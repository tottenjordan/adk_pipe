import uuid
import warnings
import pandas as pd
import re
import logging
from typing import Optional, Dict, Any

from google.genai import types
from google.adk.sessions.state import State
from google.adk.agents.callback_context import CallbackContext

from agent_common import observability, sanitize
from agent_common.rate_limit import build_rate_limit_callback

from .config import config


# --- config ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
warnings.filterwarnings("ignore")


# Shared debugging-observability callbacks (agent_common, WS3). Re-exported here
# so creative_agent/agent.py references `callbacks.<name>`, matching trend_scout.
log_empty_turn_finish_reason = observability.log_empty_turn_finish_reason
scrub_surrogates_in_response = sanitize.scrub_surrogates_in_response
log_final_state_summary = observability.make_final_state_summary(
    "creative_agent",
    (
        "combined_final_cited_report",
        "ad_copy_critique",
        "final_visual_concepts",
        "creative_evaluation_report",
    ),
)


def _set_initial_states(source: Dict[str, Any], target: State | dict[str, Any]):
    """
    Setting the initial session state given a JSON object of states.

    Args:
        source: A JSON object of states.
        target: The session state object to insert into.
    """
    unique_id = f"{str(uuid.uuid4())[:4]}"
    formatted_now = pd.Timestamp.now("UTC").strftime("%Y_%m_%d_%H_%M")
    if config.state_init not in target:
        target[config.state_init] = True
        target["gcs_bucket"] = config.GCS_BUCKET
        target["gcs_bucket_name"] = config.GCS_BUCKET_NAME
        target["agent_output_dir"] = "creative_output"
        target["gcs_folder"] = f"{formatted_now}_{unique_id}"
        logging.info(f"gcs_folder: {target['gcs_folder']}")

        target.update(source)

        # Optional product/brand reference image for image generation, supplied
        # by the caller via createSession initialState (same mechanism as
        # interactive_trend_pick). Use setdefault so the key always exists for
        # downstream .get() reads WITHOUT clobbering a caller-provided value —
        # it is deliberately NOT in `source` above, which would overwrite it.
        target.setdefault("reference_image_uri", "")

        # Optional user-supplied visual intent (image-intent-capture). Same
        # channel + rule as reference_image_uri: seeded via createSession
        # initialState, defaulted here with setdefault so the keys always exist
        # for downstream {token?} reads / .get() without clobbering caller values.
        # Deliberately NOT in `source` (which would blank a seeded value).
        for _intent_key in (
            "visual_intent",  # free-text art direction
            "brand_colors",  # palette description
            "visual_style_preference",  # preferred STYLE_PALETTE family (seed)
            "visual_avoid",  # elements to keep out (reframed positively)
            "visual_aspect_ratio",  # deterministic aspect-ratio override
            "reference_image_role",  # product | logo | style role label
        ):
            target.setdefault(_intent_key, "")


def load_session_state(callback_context: CallbackContext):
    """
    Sets up the initial state.
    Set this as a callback as before_agent_call of the `root_agent`.
    This gets called before the system instruction is constructed.

    Args:
        callback_context: The callback context.
    """
    observability.log_run_start(callback_context)

    data = {}
    data["state"] = {
        "brand": "",
        "target_product": "",
        "target_audience": "",
        "key_selling_points": "",
        "target_search_trends": "",
        # "img_artifact_keys": {"img_artifact_keys": []},
        # "vid_artifact_keys": {"vid_artifact_keys": []},
        # "final_select_ad_copies": {"final_select_ad_copies": []},
        # "final_select_vis_concepts": {"final_select_vis_concepts": []},
    }
    _set_initial_states(data["state"], callback_context.state)


# Shared query rate limiter (agent_common). Built with creative_agent's config so
# `callbacks.rate_limit_callback` keeps the same name/signature for the
# before_model_callback wiring in agent.py (and interactive_creative reuse).
rate_limit_callback = build_rate_limit_callback(config)


def collect_research_sources_callback(callback_context: CallbackContext) -> None:
    """Collects and organizes web-based research sources and their supported claims from agent events.

    This function processes the agent's `session.events` to extract web source details (URLs,
    titles, domains from `grounding_chunks`) and associated text segments with confidence scores
    (from `grounding_supports`). The aggregated source information and a mapping of URLs to short
    IDs are cumulatively stored in `callback_context.state`.

    Args:
        callback_context (CallbackContext): The context object providing access to the agent's
            session events and persistent state.
    """
    session = callback_context._invocation_context.session
    url_to_short_id = callback_context.state.get("url_to_short_id", {})
    sources = callback_context.state.get("sources", {})
    id_counter = len(url_to_short_id) + 1
    for event in session.events:
        if not (event.grounding_metadata and event.grounding_metadata.grounding_chunks):
            continue
        chunks_info = {}
        for idx, chunk in enumerate(event.grounding_metadata.grounding_chunks):
            if not chunk.web:
                continue
            url = chunk.web.uri
            title = (
                chunk.web.title
                if chunk.web.title != chunk.web.domain
                else chunk.web.domain
            )
            if url not in url_to_short_id:
                short_id = f"src-{id_counter}"
                url_to_short_id[url] = short_id
                sources[short_id] = {
                    "short_id": short_id,
                    "title": title,
                    "url": url,
                    "domain": chunk.web.domain,
                    "supported_claims": [],
                }
                id_counter += 1
            chunks_info[idx] = url_to_short_id[url]
        if event.grounding_metadata.grounding_supports:
            for support in event.grounding_metadata.grounding_supports:
                confidence_scores = support.confidence_scores or []
                chunk_indices = support.grounding_chunk_indices or []
                for i, chunk_idx in enumerate(chunk_indices):
                    if chunk_idx in chunks_info:
                        short_id = chunks_info[chunk_idx]
                        confidence = (
                            confidence_scores[i] if i < len(confidence_scores) else 0.5
                        )
                        text_segment = support.segment.text if support.segment else ""
                        sources[short_id]["supported_claims"].append(
                            {
                                "text_segment": text_segment,
                                "confidence": confidence,
                            }
                        )
    callback_context.state["url_to_short_id"] = url_to_short_id
    callback_context.state["sources"] = sources


def citation_replacement_callback(
    callback_context: CallbackContext,
) -> Optional[types.Content]:
    """Replaces citation tags in a report with Markdown-formatted links.

    Processes 'combined_final_cited_report' from context state, converting tags like
    `<cite source="src-N"/>` into hyperlinks using source information from
    `callback_context.state["sources"]`. Also fixes spacing around punctuation.

    Args:
        callback_context (CallbackContext): Contains the report and source information.

    Returns:
        types.Content: The processed report with Markdown citation links.
    """
    # types.Content: The processed report with Markdown citation links.
    final_report = callback_context.state.get("combined_final_cited_report", "")
    sources = callback_context.state.get("sources", {})

    def tag_replacer(match: re.Match) -> str:
        short_id = match.group(1)
        if not (source_info := sources.get(short_id)):
            logging.warning(f"Invalid citation tag found and removed: {match.group(0)}")
            return ""
        display_text = source_info.get("title", source_info.get("domain", short_id))
        return f" [{display_text}]({source_info['url']})"

    processed_report = re.sub(
        r'<cite\s+source\s*=\s*["\']?\s*(src-\d+)\s*["\']?\s*/>',
        tag_replacer,
        final_report,
    )
    processed_report = re.sub(r"\s+([.,;:])", r"\1", processed_report)
    callback_context.state["final_report_with_citations"] = processed_report
    # return types.Content(parts=[types.Part(text=processed_report)])
    return types.Content(parts=[types.Part(text="PDF report saved to memory 📝 !!")])
