"""
Observability Middleware

Adds per-request structured logging, Prometheus metrics, and a unique
request ID to every request/response cycle.

Emits a single log line per request containing:
  request_id, method, path, status_code, latency_ms, client_ip,
  route, upstream (if proxied), user_agent
"""

from __future__ import annotations

import time
import uuid
from typing import Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from axon.config import get_settings
from axon.observability.logging import get_logger
from axon.observability.metrics import (
    REQUEST_DURATION_SECONDS,
    REQUEST_TOTAL,
    REQUESTS_IN_FLIGHT,
)

logger = get_logger(__name__)

_NO_TRACK_PATHS = frozenset({"/health", "/ready", "/metrics", "/favicon.ico"})


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Must be the outermost application middleware so it captures total latency."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        method = request.method
        settings = get_settings()

        request_id = (
            request.headers.get(settings.observability.request_id_header)
            or str(uuid.uuid4())
        )

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=method,
            path=path,
        )

        request.state.request_id = request_id
        request.state.start_time = time.perf_counter()

        skip_metrics = path in _NO_TRACK_PATHS

        if not skip_metrics:
            REQUESTS_IN_FLIGHT.labels(method=method, route=path).inc()

        t0 = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            logger.exception("unhandled_request_exception", error=str(exc))
            raise
        finally:
            latency_ms = (time.perf_counter() - t0) * 1000

            if not skip_metrics:
                REQUESTS_IN_FLIGHT.labels(method=method, route=path).dec()
                REQUEST_TOTAL.labels(
                    method=method, route=path, status_code=status_code
                ).inc()
                REQUEST_DURATION_SECONDS.labels(method=method, route=path).observe(
                    latency_ms / 1000
                )

            log_fn = logger.warning if status_code >= 400 else logger.info
            log_fn(
                "request",
                status_code=status_code,
                latency_ms=round(latency_ms, 2),
                client_ip=_get_ip(request),
                user_agent=request.headers.get("user-agent", ""),
                slow=latency_ms > settings.observability.slow_request_threshold_ms,
            )

            structlog.contextvars.clear_contextvars()

    def _add_response_headers(
        self, response: Response, request_id: str, latency_ms: float
    ) -> None:
        settings = get_settings()
        response.headers[settings.observability.request_id_header] = request_id
        response.headers["X-Response-Time-Ms"] = f"{latency_ms:.2f}"


def _get_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
