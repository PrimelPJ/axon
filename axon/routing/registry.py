"""
Route Registry

Maintains the mapping from URL path prefixes to ReverseProxy instances.
Built at startup from the gateway config; supports hot-reload via `rebuild()`.

Matching is longest-prefix wins, consistent with how nginx and Envoy behave.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from redis.asyncio import Redis

from axon.config import RouteConfig, get_settings
from axon.observability.logging import get_logger
from axon.routing.circuit_breaker import CircuitBreaker
from axon.routing.load_balancer import Upstream, create_balancer
from axon.routing.proxy import ReverseProxy

logger = get_logger(__name__)


@dataclass
class RouteEntry:
    config: RouteConfig
    proxy: ReverseProxy
    upstreams: list[Upstream]


class RouteRegistry:
    """
    Immutable-ish registry of routes built at startup.

    In-flight requests continue to use their matched proxy even during
    a reload — the old RouteRegistry is held alive by those requests until
    they complete.
    """

    def __init__(self) -> None:
        self._routes: dict[str, RouteEntry] = {}
        # Sorted paths longest-first for prefix matching
        self._sorted_paths: list[str] = []

    def build(self, redis: Redis, http_client: httpx.AsyncClient) -> None:
        """Construct all route proxies from current settings."""
        settings = get_settings()
        self._routes = {}

        for route_cfg in settings.routes:
            upstreams = [
                Upstream(url=u.url, weight=u.weight)
                for u in route_cfg.upstreams
            ]
            lb = create_balancer(route_cfg.load_balancer, upstreams)

            cb_settings = route_cfg.circuit_breaker or settings.circuit_breaker
            circuit_breakers: dict[str, CircuitBreaker] = {}
            if cb_settings.enabled:
                for upstream in upstreams:
                    circuit_breakers[upstream.url] = CircuitBreaker(
                        redis=redis,
                        route=route_cfg.path,
                        upstream=upstream.url,
                        failure_threshold=cb_settings.failure_threshold,
                        recovery_timeout_seconds=cb_settings.recovery_timeout_seconds,
                        half_open_max_calls=cb_settings.half_open_max_calls,
                        expected_exception_codes=cb_settings.expected_exception_codes,
                    )

            proxy = ReverseProxy(
                route_config=route_cfg,
                load_balancer=lb,
                circuit_breakers=circuit_breakers,
                http_client=http_client,
            )

            self._routes[route_cfg.path] = RouteEntry(
                config=route_cfg,
                proxy=proxy,
                upstreams=upstreams,
            )
            logger.info(
                "route_registered",
                path=route_cfg.path,
                upstreams=[u.url for u in upstreams],
                strategy=route_cfg.load_balancer,
                require_auth=route_cfg.require_auth,
            )

        self._sorted_paths = sorted(self._routes.keys(), key=len, reverse=True)
        logger.info("registry_built", route_count=len(self._routes))

    def match(self, path: str) -> RouteEntry | None:
        """
        Longest-prefix match.

        Examples:
            /v1/predict/batch → matches /v1/predict before /v1
        """
        for prefix in self._sorted_paths:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return self._routes[prefix]
        return None

    def all_routes(self) -> list[RouteEntry]:
        return list(self._routes.values())

    def __len__(self) -> int:
        return len(self._routes)
