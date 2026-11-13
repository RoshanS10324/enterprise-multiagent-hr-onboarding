"""OpenTelemetry tracing. Spans: agent.run -> graph node -> llm.call / tool.call.

Exporter is chosen by OTEL_TRACES_EXPORTER: "console" for local debugging, unset = no export
(spans still correlate logs). No Azure Monitor (or other cloud) exporter is wired up -- adding one
is a real infrastructure integration, not something this fork fabricates.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_configured = False


def configure_tracing(service: str) -> None:
    """Idempotent. Call once at process start (the API factory does it)."""
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if os.environ.get("OTEL_TRACES_EXPORTER", "").lower() == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    tracer = trace.get_tracer("dact.hr.onboarding")
    with tracer.start_as_current_span(name) as sp:
        for key, value in attributes.items():
            if value is not None:
                sp.set_attribute(key, value)
        yield sp
