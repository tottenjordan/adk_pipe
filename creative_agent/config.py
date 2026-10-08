import logging
import os
from dataclasses import dataclass, field

from google.genai import errors as genai_errors
from pydantic import ValidationError

from agent_common.config import BaseAgentConfiguration
from agent_common.retry import build_infra_retry

logger = logging.getLogger(__name__)

# creative_agent calls genai directly (image gen), so it also retries the genai
# 5xx ServerError on top of the shared transient set.
INFRA_RETRY = build_infra_retry(extra_exceptions=[genai_errors.ServerError])

# Structured-output producers additionally retry a Pydantic ValidationError: at
# high temperature the model can emit invalid JSON (e.g. raw ESC control chars)
# that fails output_schema parsing, which crashed the run unretried under N=5
# concurrency (issue #104). ADK's RetryConfig matches on the exact exception class
# name, and pydantic raises `ValidationError`, so a bad sample is simply re-drawn.
# The infra set is included too (defense-in-depth for a 503 that escapes the genai
# HTTP-retry layer).
SCHEMA_RETRY = build_infra_retry(
    extra_exceptions=[genai_errors.ServerError, ValidationError]
)

# Campaign-research model for the default `global_altbucket` arm. 2026-09
# lineup refresh: gemini-3.5-flash (stable, previous-gen flash) is a DISTINCT
# per-base-model quota bucket from both trend-half models (gemini-3.8-flash
# worker / gemini-3.5-flash-lite planner) and was probed to call AND ground via
# google_search @ global. Used for the campaign planner AND worker. (Previously
# gemini-3-flash-preview, when this was only the DoE Arm C.)
ALT_GLOBAL_MODEL = "gemini-3.5-flash"

DEFAULT_CAMPAIGN_ARM = "global_altbucket"

# Creative-brief revision budget (brief_gate → brief_reviser rounds).
DEFAULT_BRIEF_REVISION_ROUNDS = 1
MAX_BRIEF_REVISION_ROUNDS = 2

# Ad-copy revision budget (copy_gate → ad_copy_reviser rounds).
DEFAULT_COPY_REVISION_ROUNDS = 1
MAX_COPY_REVISION_ROUNDS = 2

# Visual-concept fix budget (concept_gate → visual_concept_fixer rounds).
DEFAULT_CONCEPT_REVISION_ROUNDS = 1
MAX_CONCEPT_REVISION_ROUNDS = 2


# Brand history (creative_agent/brand_history.py): runs of the same brand read
# at the start of the research pipeline.
DEFAULT_BRAND_HISTORY_RUNS = 5
MAX_BRAND_HISTORY_RUNS = 20


# Rating learning (creative_agent/rating_signals.py): opted-in runs read the
# brand's human ratings (creative_ratings) at the start of the research pipeline.
RATING_LEARNING_EFFECTS: frozenset[str] = frozenset({"guidance", "styles", "checks"})
DEFAULT_RATING_LEARNING_MIN_RATINGS = 8
MAX_RATING_LEARNING_MIN_RATINGS = 200
DEFAULT_RATING_STYLE_MIN = 3
DEFAULT_RATING_REASON_MIN = 3
MAX_RATING_SAMPLE_MIN = 50
DEFAULT_RATING_LEARNING_WINDOW_DAYS = 90
MAX_RATING_LEARNING_WINDOW_DAYS = 365


def _parse_rounds(raw: str | None, default: int, maximum: int) -> int:
    try:
        value = int(raw) if raw is not None and raw.strip() else None
    except ValueError:
        value = None
    if value is None:
        return default
    return max(0, min(maximum, value))


def parse_brief_revision_rounds(raw: str | None) -> int:
    """``BRIEF_REVISION_ROUNDS`` → int clamped to 0..2; unset/blank/invalid → 1.

    The value is the maximum number of reviser passes: brief_gate routes a
    failing brief to brief_reviser, which loops back to the gate, while fewer
    than this many passes were used. 0 disables revision (issues are only
    recorded).
    """
    return _parse_rounds(raw, DEFAULT_BRIEF_REVISION_ROUNDS, MAX_BRIEF_REVISION_ROUNDS)


def parse_copy_revision_rounds(raw: str | None) -> int:
    """``COPY_REVISION_ROUNDS`` → int clamped to 0..2; unset/blank/invalid → 1.

    The maximum number of ad_copy_reviser passes: copy_gate routes final ad
    copies that fail creative_agent.copy_gate to the reviser, which loops back to
    the gate, while fewer than this many passes were used. 0 disables revision
    (issues are only recorded).
    """
    return _parse_rounds(raw, DEFAULT_COPY_REVISION_ROUNDS, MAX_COPY_REVISION_ROUNDS)


def parse_concept_revision_rounds(raw: str | None) -> int:
    """``CONCEPT_REVISION_ROUNDS`` → int clamped to 0..2; unset/blank/invalid → 1.

    The maximum number of visual_concept_fixer passes: concept_gate routes final
    visual concepts that fail creative_agent.concept_guard.concept_issues to the
    fixer, which loops back to the gate, while fewer than this many passes were
    used. 0 disables fixing (issues are only recorded).
    """
    return _parse_rounds(
        raw, DEFAULT_CONCEPT_REVISION_ROUNDS, MAX_CONCEPT_REVISION_ROUNDS
    )


def parse_brand_history_enabled(raw: str | None) -> bool:
    """``BRAND_HISTORY_ENABLED`` → bool; ON unless explicitly 0/false/no/off."""
    return (raw or "").strip().lower() not in {"0", "false", "no", "off"}


def parse_brand_history_runs(raw: str | None) -> int:
    """``BRAND_HISTORY_RUNS`` → int clamped to 0..20; unset/blank/invalid → 5.

    How many of the brand's latest runs (``creative_evals`` rows) feed the
    brand-history note; 0 disables it like ``BRAND_HISTORY_ENABLED=false``.
    """
    return _parse_rounds(raw, DEFAULT_BRAND_HISTORY_RUNS, MAX_BRAND_HISTORY_RUNS)


def _parse_bounded(raw: str | None, default: int, maximum: int) -> int:
    """Like ``_parse_rounds`` but clamped to 1..maximum (a sample size/window)."""
    return max(1, _parse_rounds(raw, default, maximum))


def parse_rating_learning_enabled(raw: str | None) -> bool:
    """``RATING_LEARNING_ENABLED`` → bool; ON (opt-in allowed) unless 0/false/no/off.

    The global kill switch: when off, no run reads ratings, even with the
    per-run ``learn_from_ratings`` toggle on.
    """
    return parse_brand_history_enabled(raw)


def parse_rating_learning_effects(raw: str | None) -> frozenset[str]:
    """``RATING_LEARNING_EFFECTS`` → the enabled effects.

    Comma-separated subset of guidance/styles/checks (case-insensitive; unknown
    items ignored). Unset → all three; an explicitly empty value → none.
    """
    if raw is None:
        return RATING_LEARNING_EFFECTS
    items = {item.strip().lower() for item in raw.split(",")}
    return frozenset(items & RATING_LEARNING_EFFECTS)


def parse_rating_learning_min_ratings(raw: str | None) -> int:
    """``RATING_LEARNING_MIN_RATINGS`` → int clamped to 1..200; default 8."""
    return _parse_bounded(
        raw, DEFAULT_RATING_LEARNING_MIN_RATINGS, MAX_RATING_LEARNING_MIN_RATINGS
    )


def parse_rating_style_min(raw: str | None) -> int:
    """``RATING_STYLE_MIN`` → int clamped to 1..50; default 3."""
    return _parse_bounded(raw, DEFAULT_RATING_STYLE_MIN, MAX_RATING_SAMPLE_MIN)


def parse_rating_reason_min(raw: str | None) -> int:
    """``RATING_REASON_MIN`` → int clamped to 1..50; default 3."""
    return _parse_bounded(raw, DEFAULT_RATING_REASON_MIN, MAX_RATING_SAMPLE_MIN)


def parse_rating_learning_window_days(raw: str | None) -> int:
    """``RATING_LEARNING_WINDOW_DAYS`` → int clamped to 1..365; default 90."""
    return _parse_bounded(
        raw, DEFAULT_RATING_LEARNING_WINDOW_DAYS, MAX_RATING_LEARNING_WINDOW_DAYS
    )


@dataclass
class ResearchConfiguration(BaseAgentConfiguration):
    """Research config for creative_agent.

    Quota spread (mirrors trend_scout PR #94, shipped as PR #101): the one
    parallel fan-out in the tree (``combined_research_pipeline``'s START →
    ``gs_sequential_planner`` / ``ca_sequential_planner``) runs the trend- and
    campaign-research pipelines at the same time. If both drive off the same
    buckets, each step fires *two* concurrent calls into one small pool →
    ``429 RESOURCE_EXHAUSTED``. Vertex quota is **per-base-model**, so the
    *campaign* half runs on a different base model from the trend half:

    - trend planner/searcher/synthesizer  → base ``lite_planner_model`` /
      ``worker_model`` (gemini-3.5-flash-lite / gemini-3.8-flash)   @ global
    - campaign planner/searcher/synthesizer → ``ALT_GLOBAL_MODEL``
      (gemini-3.5-flash)                                            @ global

    This keeps each base-model bucket at 1 concurrent caller during the parallel
    phase. (Until 2026-09 the campaign half ran on gemini-2.5 @ us-central1 — the
    ``regional_25`` arm — retired because Vertex blocks/shuts down gemini-2.5.)
    Sibling/alias names (``gemini-flash-early-exp*``) 404, so these must be
    genuine base models. The eval judge (gemini-3.1-pro-preview @ global) is
    deliberately left on its bucket — a prior A/B rejected gemini-2.5-pro there.
    """

    # Placement arm selector. Originally the quota-spread DoE seam (2026-07-17);
    # now just selects the campaign half's bucket. Default `global_altbucket`.
    campaign_research_placement: str = os.environ.get(
        "CAMPAIGN_RESEARCH_PLACEMENT", DEFAULT_CAMPAIGN_ARM
    )

    # brief_gate routes a brief that fails creative_agent.brief_check to
    # brief_reviser while fewer than this many revision rounds were used.
    # default_factory: read per instance, so tests can monkeypatch the env.
    brief_revision_rounds: int = field(
        default_factory=lambda: parse_brief_revision_rounds(
            os.getenv("BRIEF_REVISION_ROUNDS")
        )
    )

    # copy_gate routes final ad copies that fail creative_agent.copy_gate to
    # ad_copy_reviser while fewer than this many revision rounds were used.
    copy_revision_rounds: int = field(
        default_factory=lambda: parse_copy_revision_rounds(
            os.getenv("COPY_REVISION_ROUNDS")
        )
    )

    # concept_gate routes final visual concepts that fail
    # creative_agent.concept_guard.concept_issues to visual_concept_fixer while
    # fewer than this many fix rounds were used.
    concept_revision_rounds: int = field(
        default_factory=lambda: parse_concept_revision_rounds(
            os.getenv("CONCEPT_REVISION_ROUNDS")
        )
    )

    # Brand history: the brand's latest runs (creative_evals + eval reports)
    # condensed into `brand_history` for the brief writer / art director.
    brand_history_enabled: bool = field(
        default_factory=lambda: parse_brand_history_enabled(
            os.getenv("BRAND_HISTORY_ENABLED")
        )
    )
    brand_history_runs: int = field(
        default_factory=lambda: parse_brand_history_runs(
            os.getenv("BRAND_HISTORY_RUNS")
        )
    )

    # Rating learning (opt-in per run via state `learn_from_ratings`): global
    # kill switch, enabled effects, and the sample-size / window thresholds.
    rating_learning_enabled: bool = field(
        default_factory=lambda: parse_rating_learning_enabled(
            os.getenv("RATING_LEARNING_ENABLED")
        )
    )
    rating_learning_effects: frozenset[str] = field(
        default_factory=lambda: parse_rating_learning_effects(
            os.getenv("RATING_LEARNING_EFFECTS")
        )
    )
    rating_learning_min_ratings: int = field(
        default_factory=lambda: parse_rating_learning_min_ratings(
            os.getenv("RATING_LEARNING_MIN_RATINGS")
        )
    )
    rating_style_min: int = field(
        default_factory=lambda: parse_rating_style_min(os.getenv("RATING_STYLE_MIN"))
    )
    rating_reason_min: int = field(
        default_factory=lambda: parse_rating_reason_min(os.getenv("RATING_REASON_MIN"))
    )
    rating_learning_window_days: int = field(
        default_factory=lambda: parse_rating_learning_window_days(
            os.getenv("RATING_LEARNING_WINDOW_DAYS")
        )
    )

    def campaign_models(self) -> tuple[str, str, str]:
        """(lite_planner_model, worker_model, location) for the campaign pipeline.

        Resolves the campaign-research half's models per placement arm:
        - ``global_altbucket`` (default): ``ALT_GLOBAL_MODEL`` @ global — a
          distinct base-model bucket from the trend half.
        - ``global_3x``: shares the trend half's global buckets (the double-up
          baseline #101 moved away from; kept for comparison runs).

        An unrecognized value (incl. the retired ``regional_25``) degrades to
        the default ``global_altbucket``.
        """
        arms = {
            "global_3x": (self.lite_planner_model, self.worker_model, "global"),
            "global_altbucket": (ALT_GLOBAL_MODEL, ALT_GLOBAL_MODEL, "global"),
        }
        arm = self.campaign_research_placement
        if arm not in arms:
            logger.warning(
                "Unknown CAMPAIGN_RESEARCH_PLACEMENT %r; falling back to %r",
                arm,
                DEFAULT_CAMPAIGN_ARM,
            )
            return arms[DEFAULT_CAMPAIGN_ARM]
        return arms[arm]


config = ResearchConfiguration()
