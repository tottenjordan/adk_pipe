"""Vertex rejects empty strings in a response_schema enum (400 INVALID_ARGUMENT
"enum[n]: cannot be empty"), so no model-facing schema may offer "" as a choice."""

import inspect

import pytest
from pydantic import BaseModel

import creative_agent.schemas as creative_schemas
import creative_eval.schemas as eval_schemas


def _models():
    for module in (creative_schemas, eval_schemas):
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseModel) and obj.__module__ == module.__name__:
                yield obj


def _empty_enums(node, path="$"):
    if isinstance(node, dict):
        if "" in (node.get("enum") or []):
            yield path
        for key, value in node.items():
            yield from _empty_enums(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _empty_enums(value, f"{path}[{i}]")


@pytest.mark.parametrize("model", list(_models()), ids=lambda m: m.__name__)
def test_no_empty_enum_values(model):
    assert list(_empty_enums(model.model_json_schema())) == []


def test_situation_outcome_accepts_legacy_empty():
    draft = creative_schemas.TrendSituationDraft.model_validate(
        {"phase": "upcoming", "outcome": ""}
    )
    assert draft.outcome == "none"
    assert (
        creative_schemas.TrendSituationDraft.model_validate(
            {"phase": "unknown", "outcome": None}
        ).outcome
        == "none"
    )
