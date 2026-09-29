"""Opt-in Cloud Trace export for the Cloud Run backend.

``ADK_OTEL_TO_CLOUD`` (``1``/``true``/``yes``, case-insensitive; default off)
turns on ADK's ``get_fast_api_app(otel_to_cloud=True)``, which exports OTLP spans
to telemetry.googleapis.com and sets the global tracer provider. We use it rather
than ``trace_to_cloud``, whose ``cloud_trace`` exporter is not installed here.

Whether prompt/response content lands in the spans is a separate ADK knob,
``ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS`` (ADK defaults it to true) — set it to
``false`` alongside this flag in production.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes"}


def otel_to_cloud_enabled() -> bool:
    """True when ``ADK_OTEL_TO_CLOUD`` is set to a truthy value."""
    return os.getenv("ADK_OTEL_TO_CLOUD", "").strip().lower() in _TRUTHY
