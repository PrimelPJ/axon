"""
Async Reverse Proxy

Forwards incoming requests to the selected upstream, propagating headers,
body, and query parameters. Implements:
  - Automatic retries with exponential backoff on configured status codes
  - Circuit breaker integration
  - Upstream latency metrics
  - Distributed tracing header propagation (W3C traceparent)
  - Connection pooling via a shared httpx.AsyncClient per upstream
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from axon.observability.logging import get_logger
from axon.observability.metrics import (
    UPSTREAM_ERRORS_TOTAL,
    UPSTREAM_REQUEST_DURATION_SECONDS,
    UPSTREAM_REQUEST_TOTAL,
)
from axon.routing.circuit_breaker import CircuitBreaker
from axon.routing.load_balancer import LoadBalancer, LeastConnectionsBalancer

if TYPE_CHECKING:
    from axon.config import RouteConfig

logger = get_logger(__name__)

# Headers we never forward downstream (hop-by-hop + internal)
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)


def _build_upstream_headers(request: Request, upstream_url: str) -> dict[str, str]:
    """Construct headers for the upstream request."""
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP_HEADERS
    }
    # Standard forwarding headers
    client_host = request.client.host if request.client else "unknown"
    headers["X-Forwarded-For"] = headers.get("X-Forwarded-For", client_host)
    headers["X-Forwarded-Host"] = request.headers.get("host", "")
    headers["X-Forwarded-Proto"] = request.url.scheme
    headers["X-Axon-Gateway"] = "1"
    return headers


class ReverseProxy:
    """
    Forwards requests to upstream services.

    One ReverseProxy is created per route and reused across requests.
    """

    def __init__(
        self,
        route_config: "RouteConfig",
        load_balancer: LoadBalancer,
        circuit_breakers: dict[str, CircuitBreaker],
        http_client: httpx.AsyncClient,
    ) -> None:
        self._route = route_config
        self._lb = load_balancer
        self._breakers = circuit_breakers
        self._client = http_client
        self._is_lc = isinstance(load_balancer, LeastConnectionsBalancer)

    async def forward(self, request: Request) -> Response:
        """Forward the request, retrying on transient failures."""
        path = request.url.path
        if self._route.strip_prefix:
            path = path.removeprefix(self._route.path) or "/"
        query = f"?{request.url.query}" if request.url.query else ""
        body = await request.body()

        last_exc: Exception | None = None
        last_status: int = 0

        for attempt in range(self._route.retries + 1):
            upstream = self._lb.select()
            breaker = self._breakers.get(upstream.url)

            if breaker and await breaker.is_open():
                logger.warning(
                    "circuit_open_skip",
                    route=self._route.path,
                    upstream=upstream.url,
                    attempt=attempt,
                )
                continue

            target_url = f"{upstream.url.rstrip('/')}{path}{query}"
            headers = _build_upstream_headers(request, upstream.url)

            t0 = time.perf_counter()
            try:
                response = await self._client.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    content=body,
                    timeout=self._route.timeout_seconds,
                    follow_redirects=False,
                )
                latency = time.perf_counter() - t0

                UPSTREAM_REQUEST_DURATION_SECONDS.labels(
                    route=self._route.path, upstream=upstream.url
                ).observe(latency)
                UPSTREAM_REQUEST_TOTAL.labels(
                    route=self._route.path,
                    upstream=upstream.url,
                    status_code=response.status_code,
                ).inc()

                if breaker:
                    if response.status_code in self._route.retry_on:
                        await breaker.record_failure(response.status_code)
                    else:
                        await breaker.record_success()

                if response.status_code in self._route.retry_on and attempt < self._route.retries:
                    last_status = response.status_code
                    backoff = 0.1 * (2**attempt)
                    logger.info(
                        "upstream_retry",
                        route=self._route.path,
                        upstream=upstream.url,
                        status=response.status_code,
                        attempt=attempt,
                        backoff_s=backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue

                resp_headers = {
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() not in _HOP_BY_HOP_HEADERS
                }
                resp_headers["X-Upstream"] = upstream.url
                resp_headers["X-Response-Time-Ms"] = f"{latency * 1000:.2f}"

                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    headers=resp_headers,
                    media_type=response.headers.get("content-type"),
                )

            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                latency = time.perf_counter() - t0
                last_exc = exc
                error_type = type(exc).__name__

                UPSTREAM_ERRORS_TOTAL.labels(
                    route=self._route.path,
                    upstream=upstream.url,
                    error_type=error_type,
                ).inc()

                if breaker:
                    await breaker.record_failure()

                logger.warning(
                    "upstream_error",
                    route=self._route.path,
                    upstream=upstream.url,
                    error=str(exc),
                    error_type=error_type,
                    attempt=attempt,
                    latency_s=latency,
                )

                if attempt < self._route.retries:
                    await asyncio.sleep(0.1 * (2**attempt))
            finally:
                if self._is_lc and hasattr(self._lb, "release"):
                    self._lb.release(upstream)  # type: ignore[attr-defined]

        # All retries exhausted
        if last_exc:
            logger.error(
                "upstream_all_retries_failed",
                route=self._route.path,
                error=str(last_exc),
                retries=self._route.retries,
            )
            return Response(
                content=b'{"error": "upstream unavailable", "code": "UPSTREAM_UNAVAILABLE"}',
                status_code=503,
                media_type="application/json",
            )

        return Response(
            content=b'{"error": "bad gateway", "code": "BAD_GATEWAY"}',
            status_code=502,
            media_type="application/json",
        )
