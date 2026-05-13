"""
Axon Gateway — Application Entry Point

Creates the FastAPI application, wires middleware in the correct order,
registers all routes, and manages async resource lifecycle.

Middleware stack (outermost → innermost):
    ObservabilityMiddleware  — request ID, logging, metrics
    AuthMiddleware           — JWT + API key validation
    RateLimitMiddleware      — token bucket / sliding window
    [application routes]     — /health, /metrics, /_axon/*, proxy catch-all
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import ConnectionPool, Redis

from axon.auth.api_key import APIKeyRegistry
from axon.auth.jwt import JWTValidator
from axon.config import get_settings
from axon.middleware.auth import AuthMiddleware
from axon.middleware.observability import ObservabilityMiddleware
from axon.middleware.rate_limit import RateLimitMiddleware
from axon.observability.logging import configure_logging, get_logger
from axon.observability.metrics import metrics_endpoint
from axon.observability.tracing import configure_tracing
from axon.rate_limit.sliding_window import SlidingWindowRateLimiter
from axon.rate_limit.token_bucket import TokenBucketRateLimiter
from axon.routing.registry import RouteRegistry

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Manage startup and graceful shutdown of all async resources.

    Resources initialized here are stored in app.state and accessible
    from routes and middleware.
    """
    settings = get_settings()
    configure_logging()
    logger.info("axon_starting", version="0.1.0", port=settings.port)

    # ── Redis ──────────────────────────────────────────────────────────────────
    pool = ConnectionPool.from_url(
        settings.redis.url,
        max_connections=settings.redis.max_connections,
        socket_connect_timeout=settings.redis.socket_timeout_seconds,
        decode_responses=True,
    )
    redis: Redis = Redis(connection_pool=pool)

    try:
        await redis.ping()
        logger.info("redis_connected", url=settings.redis.url)
    except Exception as exc:
        logger.warning("redis_unavailable", error=str(exc))
        # Gateway can still run without Redis — rate limiting will be disabled

    # ── HTTP client (shared across all upstreams) ──────────────────────────────
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=500,
            max_keepalive_connections=100,
            keepalive_expiry=30.0,
        ),
        timeout=httpx.Timeout(30.0, connect=5.0),
        follow_redirects=False,
    )

    # ── Rate limiters ──────────────────────────────────────────────────────────
    rl_cfg = settings.rate_limit
    sliding_window = SlidingWindowRateLimiter(
        redis=redis,
        requests_per_second=rl_cfg.requests_per_second,
        burst=rl_cfg.burst,
        key_prefix=settings.redis.key_prefix + "sw:",
    )
    token_bucket = TokenBucketRateLimiter(
        redis=redis,
        requests_per_second=rl_cfg.requests_per_second,
        burst=rl_cfg.burst,
        key_prefix=settings.redis.key_prefix + "tb:",
    )

    # ── Auth ───────────────────────────────────────────────────────────────────
    jwt_validator: JWTValidator | None = None
    try:
        jwt_validator = JWTValidator(settings.auth)
        logger.info("jwt_validator_ready", algorithm=settings.auth.jwt_algorithm)
    except (ValueError, FileNotFoundError) as exc:
        logger.warning("jwt_disabled", reason=str(exc))

    api_key_registry = APIKeyRegistry(redis=redis, key_prefix=settings.redis.key_prefix + "apikey:")

    # ── Route registry ─────────────────────────────────────────────────────────
    registry = RouteRegistry()
    registry.build(redis=redis, http_client=http_client)

    # ── Store in app.state ─────────────────────────────────────────────────────
    app.state.redis = redis
    app.state.http_client = http_client
    app.state.registry = registry
    app.state.jwt_validator = jwt_validator
    app.state.api_key_registry = api_key_registry
    app.state.sliding_window = sliding_window
    app.state.token_bucket = token_bucket

    configure_tracing(app)

    logger.info(
        "axon_ready",
        routes=len(registry),
        auth_jwt=jwt_validator is not None,
        rate_limiting=rl_cfg.enabled,
    )

    yield  # ── Application running ──────────────────────────────────────────

    # ── Graceful shutdown ──────────────────────────────────────────────────────
    logger.info("axon_shutting_down")
    await http_client.aclose()
    await redis.aclose()
    await pool.aclose()
    logger.info("axon_stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Axon Gateway",
        description="ML-native API gateway with auth, adaptive routing, and full observability",
        version="0.1.0",
        docs_url="/_axon/docs" if settings.debug else None,
        redoc_url="/_axon/redoc" if settings.debug else None,
        openapi_url="/_axon/openapi.json" if settings.debug else None,
        lifespan=lifespan,
    )

    # ── Middleware (registered in reverse — last registered = outermost) ────────
    # Important: RateLimitMiddleware must come after AuthMiddleware so identity
    # is available for api_key/user_id key functions.

    @app.middleware("http")
    async def rate_limit_mw(request: Request, call_next: Any) -> Any:
        mw = RateLimitMiddleware(
            app=call_next,
            sliding_window=request.app.state.sliding_window,
            token_bucket=request.app.state.token_bucket,
        )
        return await mw.dispatch(request, call_next)

    @app.middleware("http")
    async def auth_mw(request: Request, call_next: Any) -> Any:
        mw = AuthMiddleware(
            app=call_next,
            jwt_validator=request.app.state.jwt_validator,
            api_key_registry=request.app.state.api_key_registry,
        )
        return await mw.dispatch(request, call_next)

    app.add_middleware(ObservabilityMiddleware)

    # ── Built-in routes ────────────────────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["system"])
    async def ready(request: Request) -> dict[str, object]:
        try:
            await request.app.state.redis.ping()
            redis_ok = True
        except Exception:
            redis_ok = False
        return {
            "status": "ready" if redis_ok else "degraded",
            "redis": redis_ok,
            "routes": len(request.app.state.registry),
        }

    @app.get("/_axon/status", tags=["admin"])
    async def gateway_status(request: Request) -> dict[str, object]:
        registry: RouteRegistry = request.app.state.registry
        return {
            "version": "0.1.0",
            "routes": [
                {
                    "path": e.config.path,
                    "upstreams": [u.url for u in e.upstreams],
                    "strategy": e.config.load_balancer,
                    "require_auth": e.config.require_auth,
                }
                for e in registry.all_routes()
            ],
        }

    app.add_route(
        path=get_settings().observability.metrics_path,
        route=metrics_endpoint,
    )

    # ── Proxy catch-all ────────────────────────────────────────────────────────
    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def proxy(request: Request) -> Any:
        registry: RouteRegistry = request.app.state.registry
        path = request.url.path
        entry = registry.match(path)

        if entry is None:
            return JSONResponse(
                status_code=404,
                content={"error": "no_route", "path": path},
            )

        if request.method not in entry.config.allowed_methods:
            return JSONResponse(
                status_code=405,
                content={"error": "method_not_allowed", "allowed": entry.config.allowed_methods},
            )

        return await entry.proxy.forward(request)

    return app


app = create_app()
