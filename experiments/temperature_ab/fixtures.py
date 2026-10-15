"""Fixed inputs for the Gemini 3 temperature A/B.

Two campaigns copied from ``tests/eval/evalsets/creative_agent_evalset.json``
(PRS Guitars; Estee Lauder Advanced Night Repair) — drift-tested against it in
``tests/test_temperature_ab.py``. Each carries an evergreen trend so research
and the timely slot don't age between runs.

The initial state mirrors the frontend's ``buildInitialState``
(``frontend/src/lib/initial-state.ts``): ``ui_app`` plus, for the creative
agents, the campaign keys the callbacks only ``setdefault``. trend_scout gets
only ``ui_app`` (auto pick: ``interactive_trend_pick`` stays unset) and reads
the campaign from the kickoff message, like the frontend's run.
"""

from __future__ import annotations

from dataclasses import dataclass

EXPERIMENT_USER_ID = "temperature-ab@jordantotten.altostrat.com"
APPS = ("creative_agent", "trend_scout")


@dataclass(frozen=True)
class Campaign:
    key: str
    brand: str
    target_audience: str
    target_product: str
    key_selling_points: str
    trend: str


CAMPAIGNS: tuple[Campaign, ...] = (
    Campaign(
        key="prs",
        brand="Paul Reed Smith (PRS)",
        target_audience=(
            "millennials who follow jam bands (e.g., Widespread Panic and Phish), "
            "respond positively to nostalgic messages, and love surreal memes"
        ),
        target_product="PRS SE CE24 Electric Guitar",
        key_selling_points=(
            "The 85/15 S Humbucker pickups deliver a wide tonal range, from thick "
            "humbucker tones to clear single-coil sounds, making the guitar "
            "suitable for various genres."
        ),
        trend="viral guitar solo covers on TikTok",
    ),
    Campaign(
        key="anr",
        brand="Estee Lauder",
        target_audience=(
            "Gen-Z gamers (18-25) who spend 4+ hours daily gaming, care about skin "
            "health but find traditional skincare messaging boring and irrelevant "
            "to their lifestyle"
        ),
        target_product="Advanced Night Repair Serum",
        key_selling_points=(
            "Repairs visible signs of damage from blue light exposure and late "
            "nights. 7-in-1 serum powered by Chronolux Power Signal Technology for "
            "visibly reduced lines, wrinkles, and even skin tone."
        ),
        trend="blue light skincare for gamers",
    ),
)

CAMPAIGNS_BY_KEY = {c.key: c for c in CAMPAIGNS}


def _check_app(app: str) -> None:
    if app not in APPS:
        raise ValueError(f"unsupported app {app!r}; expected one of {APPS}")


def initial_state(app: str, campaign: Campaign) -> dict:
    """The ``createSession`` initial state the frontend would seed for ``app``."""
    _check_app(app)
    state: dict = {"ui_app": app}
    if app == "trend_scout":
        return state
    state.update(
        {
            "brand": campaign.brand,
            "target_audience": campaign.target_audience,
            "target_product": campaign.target_product,
            "key_selling_points": campaign.key_selling_points,
            "target_search_trends": campaign.trend,
        }
    )
    return state


def kickoff_message(app: str, campaign: Campaign) -> str:
    """The readable kickoff message, formatted like ``frontend/src/app/page.tsx``."""
    _check_app(app)
    message = (
        f'Brand Name: "{campaign.brand}"\n'
        f'Target Audience: "{campaign.target_audience}"\n'
        f'Target Product: "{campaign.target_product}"\n'
        f'Key Selling Points: "{campaign.key_selling_points}"'
    )
    if app == "creative_agent":
        message += f'\ntarget_search_trends: "{campaign.trend}"'
    return message
