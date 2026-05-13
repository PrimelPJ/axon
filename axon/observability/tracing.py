"""
OpenTelemetry tracing configuration for Axon Gateway.

Exports spans via OTLP gRPC to a configured collector (Jaeger, Tempo, etc.).
Span attributes follow OTel semantic conventions where applicable.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

if TYPE_CHECKING:
    from fastapi import FastAPI

from axon.config import get_settings
from axon.observability.logging import get_logger

logger = get_logger(__name__)


def configure_tracing(app: "FastAPI") -> None:
    settings = get_settings()
    if not settings.observability.tracing_enabled:
        return

    resource = Resource.create(
        {
            "service.name": settings.observability.service_name,
            "service.version": "0.1.0",
            "deployment.environment": "production" if not settings.debug else "development",
        }
    )

    provider = TracerProvider(resource=resource)

    if settings.observability.otlp_endpoint:
        try:
            exporter = OTLPSpanExporter(endpoint=settings.observability.otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info(
                "tracing_configured",
                endpoint=settings.observability.otlp_endpoint,
            )
        except Exception as exc:
            logger.warning("tracing_setup_failed", error=str(exc))
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name, tracer_provider=trace.get_tracer_provider())


@asynccontextmanager
async def upstream_span(
    tracer: trace.Tracer,
    route: str,
    upstream_url: str,
    method: str,
) -> AsyncIterator[trace.Span]:
    """Context manager for tracing upstream calls with semantic attributes."""
    with tracer.start_as_current_span(
        f"upstream.{method.lower()}",
        kind=trace.SpanKind.CLIENT,
    ) as span:
        span.set_attribute("http.method", method)
        span.set_attribute("http.url", upstream_url)
        span.set_attribute("axon.route", route)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise
