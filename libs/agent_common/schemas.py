"""Shared Pydantic schemas for graph-Workflow pipelines."""

from pydantic import BaseModel, Field


# The ``input_schema`` a pipeline node exposes when called as a tool.
# ``NodeTool`` requires an explicit ``input_schema``; this one reproduces the
# ``AgentTool`` call shape (a single required ``request`` string), so a root
# agent's tool declaration is unchanged when an ``AgentTool`` pipeline becomes a
# bare graph node in ``tools=[...]``. Deliberately no class docstring: pydantic
# would put it in the JSON schema as the parameters ``description`` that the
# model sees.
class PipelineRequest(BaseModel):
    request: str = Field(
        description="The natural-language task for the pipeline to perform."
    )
