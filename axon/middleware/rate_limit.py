"""
Rate Limiting Middleware

Evaluates incoming requests against per-client rate limits before forwarding.
Adds standard RateLimit headers to every response (whether allowed or denied).

Key functions:
    ip       — client IP (default for unauthenticated routes)
    api_key  — authenticated API key ID
    user_id  — subject from JWT

Per-route limits override the global config. API keys can carry their own
rate_limit_override which supersedes both global and per-route limits.
"""

from __future__ import annotations

import math
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from axon.config import RateLimitConfig, RateLimitAlgorithm, get_settings
from axon.observability.logging import get_logger
from axon.observability.metrics import RATE_LIMIT_REMAINING, RATE_LIMIT_TOTAL
from axon.rate_limit.sliding_window import SlidingWindowRateLimiter
from axon.rate_limit.token_bucket import TokenBucketRateLimiter

logger = get_logger(__name__)

_ALWAYS_EXEMPT: frozenset[str] = frozenset({"/health", "/ready", "/metrics", "/_axon/status"})


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _rate_limit_key(request: Request, key_func: str) -> str:
    """Build the per-client rate limit key based on strategy."""
    if key_func == "api_key":
        identity = getattr(request.state, "identity", None)
        if identity and identity.auth_method == "api_key":
            return f"ak:{identity.subject}"
    if key_func == "user_id":
        identity = getattr(request.state, "identity", None)
        if identity:
            return f"uid:{identity.subject}"
    return f"ip:{_client_ip(request)}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Rate limiting middleware. Must run AFTER AuthMiddleware so that
    identity is available for key_func=api_key/user_id.
    """

    def __init__(
        self,
        app: Callable,
        sliding_window: SlidingWindowRateLimiter,
        token_bucket: TokenBucketRateLimiter,
    ) -> None:
        super().__init__(app)
        self._sw = sliding_window
        self._tb = token_bucket

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in _ALWAYS_EXEMPT:
            return await call_next(request)

        settings = get_settings()
        cfg = settings.rate_limit

        # Check for per-route override
        for route in settings.routes:
            if path == route.path or path.startswith(route.path.rstrip("/") + "/"):
                if route.rate_limit_override:
                    cfg = route.rate_limit_override
                break

        if not cfg.enabled:
            return await call_next(request)

        # Check if authenticated user has personal rate limit override
        identity = getattr(request.state, "identity", None)
        if identity and identity.rate_limit_override:
            cfg = RateLimitConfig(
                enabled=True,
                algorithm=cfg.algorithm,
                requests_per_second=float(identity.rate_limit_override),
                burst=identity.rate_limit_override * 2,
                key_func=cfg.key_func,
            )

        key = f"{path}:{_rate_limit_key(request, cfg.key_func)}"

        if cfg.algorithm == RateLimitAlgorithm.TOKEN_BUCKET:
            result = await self._tb.check(key)
            allowed = result.allowed
            remaining = result.tokens_remaining
            limit = result.capacity
            retry_after_ms = result.retry_after_ms
        else:
            result_sw = await self._sw.check(key)
            allowed = result_sw.allowed
            remaining = result_sw.remaining
            limit = result_sw.limit
            retry_after_ms = result_sw.retry_after_ms

        RATE_LIMIT_TOTAL.labels(route=path, result="allowed" if allowed else "rejected").inc()
        RATE_LIMIT_REMAINING.labels(route=path, key_type=cfg.key_func).set(remaining)

        if not allowed:
            logger.info(
                "rate_limit_rejected",
                path=path,
                key=key,
                limit=limit,
                retry_after_ms=retry_after_ms,
            )
            retry_after_s = math.ceil(retry_after_ms / 1000)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "detail": f"Too many requests. Retry after {retry_after_s}s.",
                    "retry_after_seconds": retry_after_s,
                },
                headers={
                    "Retry-After": str(retry_after_s),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(retry_after_s),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
