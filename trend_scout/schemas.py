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
