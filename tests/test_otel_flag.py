"""Opt-in Cloud Trace export for the Cloud Run backend (``ADK_OTEL_TO_CLOUD``).

``runserver.otel.otel_to_cloud_enabled`` parses the env flag; the wiring into
``deployment/async_app.py``'s ``get_fast_api_app`` call is checked statically
(importing async_app builds the whole ADK server + authz, too heavy here).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from runserver.otel import otel_to_cloud_enabled

ASYNC_APP = pathlib.Path(__file__).resolve().parents[1] / "deployment" / "async_app.py"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("false", False),
        ("0", False),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("TRUE", True),
        (" true ", True),
    ],
)
def test_otel_to_cloud_enabled(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("ADK_OTEL_TO_CLOUD", raising=False)
    else:
        monkeypatch.setenv("ADK_OTEL_TO_CLOUD", value)
    assert otel_to_cloud_enabled() is expected


def test_async_app_passes_flag_to_get_fast_api_app():
    tree = ast.parse(ASYNC_APP.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get_fast_api_app"
    ]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert "otel_to_cloud" in kw
    value = kw["otel_to_cloud"]
    assert isinstance(value, ast.Call)
    assert isinstance(value.func, ast.Name)
    assert value.func.id == "otel_to_cloud_enabled"
    # trace_to_cloud's cloud_trace exporter isn't installed; never use it.
    assert "trace_to_cloud" not in kw
