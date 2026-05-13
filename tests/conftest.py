"""
Shared pytest fixtures.

Uses fakeredis for Redis, respx for HTTP mocking, and an in-memory
FastAPI test client. All fixtures are function-scoped by default.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import fakeredis.aioredis as fakeredis
from httpx import AsyncClient, ASGITransport

from axon.auth.api_key import APIKeyRegistry
from axon.auth.jwt import JWTValidator
from axon.config import (
    AuthConfig,
    CircuitBreakerConfig,
    ObservabilityConfig,
    RateLimitConfig,
    RedisConfig,
    RouteConfig,
    Settings,
    UpstreamConfig,
    override_settings,
)
from axon.rate_limit.sliding_window import SlidingWindowRateLimiter
from axon.rate_limit.token_bucket import TokenBucketRateLimiter
from axon.routing.circuit_breaker import CircuitBreaker


JWT_SECRET = "test-secret-key-for-axon-unit-tests"


@pytest.fixture
def settings(upstream_url: str = "http://fake-upstream") -> Settings:
    s = Settings(
        debug=True,
        auth=AuthConfig(
            jwt_secret=JWT_SECRET,
            jwt_algorithm="HS256",
        ),
        redis=RedisConfig(url="redis://localhost:6379/15"),
        rate_limit=RateLimitConfig(
            enabled=True,
            requests_per_second=1000.0,
            burst=2000,
        ),
        circuit_breaker=CircuitBreakerConfig(enabled=True, failure_threshold=3),
        observability=ObservabilityConfig(log_format="console"),
        routes=[
            RouteConfig(
                path="/v1/predict",
                upstreams=[UpstreamConfig(url="http://fake-upstream")],
                require_auth=True,
            ),
            RouteConfig(
                path="/public/ping",
                upstreams=[UpstreamConfig(url="http://fake-upstream")],
                require_auth=False,
            ),
        ],
    )
    override_settings(s)
    return s


@pytest_asyncio.fixture
async def redis() -> fakeredis.FakeRedis:
    r = fakeredis.FakeRedis(decode_responses=True)
    yield r
    await r.flushall()
    await r.aclose()


@pytest_asyncio.fixture
async def sliding_window(redis: fakeredis.FakeRedis) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(
        redis=redis,
        requests_per_second=10.0,
        burst=10,
        key_prefix="test:sw:",
    )


@pytest_asyncio.fixture
async def token_bucket(redis: fakeredis.FakeRedis) -> TokenBucketRateLimiter:
    return TokenBucketRateLimiter(
        redis=redis,
        requests_per_second=10.0,
        burst=10,
        key_prefix="test:tb:",
    )


@pytest_asyncio.fixture
async def api_key_registry(redis: fakeredis.FakeRedis) -> APIKeyRegistry:
    return APIKeyRegistry(redis=redis, key_prefix="test:apikey:")


@pytest.fixture
def jwt_validator() -> JWTValidator:
    return JWTValidator(AuthConfig(jwt_secret=JWT_SECRET, jwt_algorithm="HS256"))


@pytest_asyncio.fixture
async def circuit_breaker(redis: fakeredis.FakeRedis) -> CircuitBreaker:
    return CircuitBreaker(
        redis=redis,
        route="/v1/predict",
        upstream="http://fake-upstream",
        failure_threshold=3,
        recovery_timeout_seconds=5.0,
        key_prefix="test:cb:",
    )
