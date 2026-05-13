"""
Prometheus metrics for Axon Gateway.

All metrics are registered at module import time and reused across requests.
Labels follow OpenMetrics conventions: snake_case, no high-cardinality values.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response

# ─── Request metrics ───────────────────────────────────────────────────────────

REQUEST_TOTAL = Counter(
    "axon_requests_total",
    "Total number of HTTP requests processed",
    ["method", "route", "status_code"],
)

REQUEST_DURATION_SECONDS = Histogram(
    "axon_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

REQUEST_SIZE_BYTES = Histogram(
    "axon_request_size_bytes",
    "HTTP request body size in bytes",
    ["method", "route"],
    buckets=(64, 256, 1024, 4096, 16384, 65536, 262144, 1048576),
)

RESPONSE_SIZE_BYTES = Histogram(
    "axon_response_size_bytes",
    "HTTP response body size in bytes",
    ["method", "route", "status_code"],
    buckets=(64, 256, 1024, 4096, 16384, 65536, 262144, 1048576),
)

REQUESTS_IN_FLIGHT = Gauge(
    "axon_requests_in_flight",
    "Number of HTTP requests currently being processed",
    ["method", "route"],
)

# ─── Rate limiting metrics ─────────────────────────────────────────────────────

RATE_LIMIT_TOTAL = Counter(
    "axon_rate_limit_total",
    "Total number of rate limit decisions",
    ["route", "result"],  # result: allowed | rejected
)

RATE_LIMIT_REMAINING = Gauge(
    "axon_rate_limit_remaining_tokens",
    "Remaining tokens/requests for current window (sampled)",
    ["route", "key_type"],
)

# ─── Auth metrics ──────────────────────────────────────────────────────────────

AUTH_TOTAL = Counter(
    "axon_auth_total",
    "Total number of authentication attempts",
    ["method", "result"],  # method: jwt | api_key; result: success | failure
)

# ─── Circuit breaker metrics ───────────────────────────────────────────────────

CIRCUIT_BREAKER_STATE = Gauge(
    "axon_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=open, 2=half_open)",
    ["route", "upstream"],
)

CIRCUIT_BREAKER_TRANSITIONS = Counter(
    "axon_circuit_breaker_transitions_total",
    "Total circuit breaker state transitions",
    ["route", "upstream", "from_state", "to_state"],
)

# ─── Upstream metrics ──────────────────────────────────────────────────────────

UPSTREAM_REQUEST_TOTAL = Counter(
    "axon_upstream_requests_total",
    "Total requests forwarded to upstream services",
    ["route", "upstream", "status_code"],
)

UPSTREAM_REQUEST_DURATION_SECONDS = Histogram(
    "axon_upstream_request_duration_seconds",
    "Upstream request latency in seconds",
    ["route", "upstream"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

UPSTREAM_ERRORS_TOTAL = Counter(
    "axon_upstream_errors_total",
    "Total upstream errors",
    ["route", "upstream", "error_type"],
)

# ─── Connection pool metrics ────────────────────────────────────────────────────

REDIS_POOL_CONNECTIONS = Gauge(
    "axon_redis_pool_connections",
    "Current Redis connection pool size",
    ["state"],  # state: active | idle
)


# ─── Metrics endpoint ──────────────────────────────────────────────────────────

async def metrics_endpoint(_request: Request) -> Response:
    """Expose Prometheus metrics at /metrics."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
