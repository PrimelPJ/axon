"""
Tests for routing components.

Covers: circuit breaker state transitions, load balancer strategies,
route registry matching, and proxy header forwarding.
"""

from __future__ import annotations

import pytest

from axon.routing.circuit_breaker import CBState, CircuitBreaker
from axon.routing.load_balancer import (
    LeastConnectionsBalancer,
    RoundRobinBalancer,
    Upstream,
    WeightedRoundRobinBalancer,
    create_balancer,
)
from axon.routing.registry import RouteRegistry


# ─── Circuit Breaker ──────────────────────────────────────────────────────────

class TestCircuitBreaker:
    async def test_starts_closed(self, circuit_breaker: CircuitBreaker) -> None:
        assert not await circuit_breaker.is_open()

    async def test_opens_after_failure_threshold(self, circuit_breaker: CircuitBreaker) -> None:
        for _ in range(3):
            await circuit_breaker.record_failure(status_code=503)
        assert await circuit_breaker.is_open()

    async def test_does_not_open_before_threshold(self, circuit_breaker: CircuitBreaker) -> None:
        for _ in range(2):  # threshold is 3
            await circuit_breaker.record_failure(status_code=503)
        assert not await circuit_breaker.is_open()

    async def test_success_resets_failure_count(self, circuit_breaker: CircuitBreaker) -> None:
        await circuit_breaker.record_failure(status_code=502)
        await circuit_breaker.record_failure(status_code=502)
        await circuit_breaker.record_success()
        # Should need full threshold again to open
        await circuit_breaker.record_failure(status_code=502)
        await circuit_breaker.record_failure(status_code=502)
        assert not await circuit_breaker.is_open()

    async def test_ignores_non_error_codes(self, circuit_breaker: CircuitBreaker) -> None:
        for _ in range(10):
            await circuit_breaker.record_failure(status_code=200)
        assert not await circuit_breaker.is_open()

    async def test_manual_reset_closes_open_breaker(self, circuit_breaker: CircuitBreaker) -> None:
        for _ in range(3):
            await circuit_breaker.record_failure(status_code=503)
        assert await circuit_breaker.is_open()
        await circuit_breaker.reset()
        assert not await circuit_breaker.is_open()

    async def test_status_returns_state_info(self, circuit_breaker: CircuitBreaker) -> None:
        status = await circuit_breaker.get_status()
        assert status["state"] == "closed"
        assert "failure_threshold" in status
        assert status["failure_threshold"] == 3


# ─── Load Balancers ───────────────────────────────────────────────────────────

def make_upstreams(n: int, weights: list[int] | None = None) -> list[Upstream]:
    return [
        Upstream(url=f"http://upstream-{i}", weight=weights[i] if weights else 1)
        for i in range(n)
    ]


class TestRoundRobin:
    def test_cycles_through_all_upstreams(self) -> None:
        upstreams = make_upstreams(3)
        lb = RoundRobinBalancer(upstreams)
        selected = [lb.select().url for _ in range(6)]
        # Should visit all 3, then repeat
        assert selected[:3] == [u.url for u in upstreams]
        assert selected[3:] == selected[:3]

    def test_single_upstream(self) -> None:
        lb = RoundRobinBalancer(make_upstreams(1))
        assert lb.select().url == "http://upstream-0"

    def test_skips_unhealthy(self) -> None:
        upstreams = make_upstreams(3)
        lb = RoundRobinBalancer(upstreams)
        lb.mark_unhealthy("http://upstream-1")
        for _ in range(10):
            assert lb.select().url != "http://upstream-1"

    def test_raises_when_all_unhealthy(self) -> None:
        upstreams = make_upstreams(2)
        lb = RoundRobinBalancer(upstreams)
        for u in upstreams:
            lb.mark_unhealthy(u.url)
        with pytest.raises(RuntimeError, match="No healthy"):
            lb.select()


class TestWeightedRoundRobin:
    def test_higher_weight_selected_more(self) -> None:
        upstreams = make_upstreams(2, weights=[3, 1])
        lb = WeightedRoundRobinBalancer(upstreams)
        selections = [lb.select().url for _ in range(400)]
        count_0 = selections.count("http://upstream-0")
        count_1 = selections.count("http://upstream-1")
        # Roughly 3:1 ratio
        assert count_0 > count_1 * 2
        assert count_0 + count_1 == 400

    def test_even_weights_behave_like_round_robin(self) -> None:
        upstreams = make_upstreams(2, weights=[1, 1])
        lb = WeightedRoundRobinBalancer(upstreams)
        selections = [lb.select().url for _ in range(100)]
        count_0 = selections.count("http://upstream-0")
        count_1 = selections.count("http://upstream-1")
        assert abs(count_0 - count_1) <= 5  # within 5% variance


class TestLeastConnections:
    def test_selects_least_loaded(self) -> None:
        upstreams = make_upstreams(3)
        lb = LeastConnectionsBalancer(upstreams)
        upstreams[0].active_connections = 10
        upstreams[1].active_connections = 2
        upstreams[2].active_connections = 5
        selected = lb.select()
        assert selected.url == "http://upstream-1"
        assert selected.active_connections == 3  # incremented

    def test_release_decrements_counter(self) -> None:
        upstreams = make_upstreams(1)
        lb = LeastConnectionsBalancer(upstreams)
        u = lb.select()
        assert u.active_connections == 1
        lb.release(u)
        assert u.active_connections == 0

    def test_release_does_not_go_negative(self) -> None:
        upstreams = make_upstreams(1)
        lb = LeastConnectionsBalancer(upstreams)
        lb.release(upstreams[0])  # release without select
        assert upstreams[0].active_connections == 0


class TestCreateBalancer:
    def test_creates_round_robin(self) -> None:
        lb = create_balancer("round_robin", make_upstreams(2))
        assert isinstance(lb, RoundRobinBalancer)

    def test_creates_weighted(self) -> None:
        lb = create_balancer("weighted_round_robin", make_upstreams(2, weights=[2, 1]))
        assert isinstance(lb, WeightedRoundRobinBalancer)

    def test_invalid_strategy_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown"):
            create_balancer("telepathy", make_upstreams(1))


# ─── Route Registry ───────────────────────────────────────────────────────────

class TestRouteRegistry:
    def test_longest_prefix_match(self) -> None:
        registry = RouteRegistry()
        registry._routes = {
            "/v1": object(),  # type: ignore
            "/v1/predict": object(),  # type: ignore
            "/v1/predict/batch": object(),  # type: ignore
        }
        registry._sorted_paths = sorted(registry._routes.keys(), key=len, reverse=True)

        assert registry.match("/v1/predict/batch/stream") is registry._routes["/v1/predict/batch"]
        assert registry.match("/v1/predict/single") is registry._routes["/v1/predict"]
        assert registry.match("/v1/models") is registry._routes["/v1"]

    def test_no_match_returns_none(self) -> None:
        registry = RouteRegistry()
        registry._routes = {}
        registry._sorted_paths = []
        assert registry.match("/nonexistent") is None

    def test_exact_path_match(self) -> None:
        registry = RouteRegistry()
        sentinel = object()
        registry._routes = {"/exact": sentinel}  # type: ignore
        registry._sorted_paths = ["/exact"]
        assert registry.match("/exact") is sentinel
