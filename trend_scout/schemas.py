"""Output schemas for trend_scout sub-agents."""

from pydantic import BaseModel, Field


class TrendFlag(BaseModel):
    """Sensitivity verdict for one raw trend term."""

    term: str = Field(description="The raw trend term, copied verbatim.")
    sensitive: bool = Field(
        default=False,
        description=(
            "True only when search interest centres on a death, tragedy, disaster, "
            "violence, crime or a real person's serious illness or injury."
        ),
    )
    reason: str = Field(
        default="",
        description="Short neutral reason when sensitive; empty otherwise.",
    )


class TrendFlags(BaseModel):
    """One entry per raw trend term (state `trend_flags`)."""

    flags: list[TrendFlag] = Field(default_factory=list)


class TrendSummary(BaseModel):
    """Why one raw trend term is trending today, in one neutral sentence."""

    term: str = Field(description="The raw trend term, copied verbatim.")
    summary: str = Field(
        default="",
        description=(
            "One neutral, factual sentence of 25 words or fewer on why the term "
            "is being searched today; no speculation, jokes or graphic detail."
        ),
    )


class TrendSummaries(BaseModel):
    """The describe step's draft (state `trend_summaries_draft`)."""

    items: list[TrendSummary] = Field(default_factory=list)


class AnalyzedTrend(BaseModel):
    """The synthesizer's research for one trend term (an `info_gtrends` item).

    The field names are the JSON keys every reader already parses:
    ``trend_context._info_item`` (context / cultural_angle / event_hint), the
    picker's ``{info_gtrends?}`` and the frontend trend cards
    (``components/trend-cards.tsx`` ``researchItem``). Everything but the term
    defaults to "" so a sparse finding still validates.
    """

    term: str = Field(description="The search term, as given in the findings.")
    category: str = Field(
        default="",
        description="Broad category (e.g. Sports, Pop Culture, Politics).",
    )
    context: str = Field(default="", description="Brief explanation of what happened.")
    cultural_angle: str = Field(
        default="",
        description="Why this matters to culture right now, as one short phrase.",
    )
    as_of: str = Field(default="", description="Today's date, copied as given.")
    event_hint: str = Field(
        default="",
        description=(
            "One factual line about the next or last notable dated event from the "
            "findings, or an empty string when they give none."
        ),
    )


class AnalyzedTrends(BaseModel):
    """The synthesizer's structured briefing (state `info_gtrends`)."""

    analyzed_trends: list[AnalyzedTrend] = Field(default_factory=list)
