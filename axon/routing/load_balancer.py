"""
Load Balancer implementations for upstream selection.

Supported strategies:
    - RoundRobinBalancer       — simple cyclic selection, O(1)
    - WeightedRoundRobinBalancer — smooth WRR (Nginx algorithm), O(N)
    - LeastConnectionsBalancer — routes to upstream with fewest active requests
    - RandomBalancer            — uniform random selection, O(1)

All balancers are stateful but thread-safe (asyncio single-threaded event loop).
For multi-process deployments, use LeastConnectionsBalancer with Redis counters
(or WeightedRoundRobin / RoundRobin, which are stateless per replica).
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Upstream:
    url: str
    weight: int = 1
    # Runtime state (not serialized)
    active_connections: int = field(default=0, compare=False, repr=False)
    _current_weight: int = field(default=0, init=False, compare=False, repr=False)
    healthy: bool = field(default=True, compare=False, repr=False)

    def effective_weight(self) -> int:
        return self.weight if self.healthy else 0


class LoadBalancer(ABC):
    """Abstract base — all balancers implement `select()`."""

    def __init__(self, upstreams: list[Upstream]) -> None:
        if not upstreams:
            raise ValueError("LoadBalancer requires at least one upstream")
        self._upstreams = upstreams

    @abstractmethod
    def select(self) -> Upstream:
        """Return the next upstream to use."""
        ...

    def mark_healthy(self, url: str) -> None:
        for u in self._upstreams:
            if u.url == url:
                u.healthy = True

    def mark_unhealthy(self, url: str) -> None:
        for u in self._upstreams:
            if u.url == url:
                u.healthy = False

    @property
    def healthy_upstreams(self) -> list[Upstream]:
        return [u for u in self._upstreams if u.healthy]

    def all_status(self) -> list[dict[str, object]]:
        return [
            {
                "url": u.url,
                "weight": u.weight,
                "healthy": u.healthy,
                "active_connections": u.active_connections,
            }
            for u in self._upstreams
        ]


class RoundRobinBalancer(LoadBalancer):
    """Simple round-robin with O(1) selection."""

    def __init__(self, upstreams: list[Upstream]) -> None:
        super().__init__(upstreams)
        self._index = 0

    def select(self) -> Upstream:
        healthy = self.healthy_upstreams
        if not healthy:
            raise RuntimeError("No healthy upstreams available")
        upstream = healthy[self._index % len(healthy)]
        self._index = (self._index + 1) % len(healthy)
        return upstream


class WeightedRoundRobinBalancer(LoadBalancer):
    """
    Nginx-style smooth weighted round-robin.

    Each upstream gets selected proportional to its weight while maintaining
    even distribution over time. Never sends N consecutive requests to the
    highest-weight upstream.

    Algorithm:
        For each request:
        1. current_weight[i] += effective_weight[i]
        2. best = upstream with highest current_weight
        3. current_weight[best] -= sum(all effective_weights)
    """

    def select(self) -> Upstream:
        healthy = self.healthy_upstreams
        if not healthy:
            raise RuntimeError("No healthy upstreams available")

        total = sum(u.effective_weight() for u in healthy)
        if total == 0:
            raise RuntimeError("All upstream weights are zero")

        for u in healthy:
            u._current_weight += u.effective_weight()

        best = max(healthy, key=lambda u: u._current_weight)
        best._current_weight -= total
        return best


class LeastConnectionsBalancer(LoadBalancer):
    """
    Routes to the upstream with the fewest active in-flight requests.

    Requires callers to call `release()` when a request completes so the
    counter is accurate. Best suited for long-lived or variable-duration requests.
    """

    def select(self) -> Upstream:
        healthy = self.healthy_upstreams
        if not healthy:
            raise RuntimeError("No healthy upstreams available")
        # Break ties with weight: prefer higher weight when connections are equal
        best = min(
            healthy,
            key=lambda u: (u.active_connections, -u.weight),
        )
        best.active_connections += 1
        return best

    def release(self, upstream: Upstream) -> None:
        upstream.active_connections = max(0, upstream.active_connections - 1)


class RandomBalancer(LoadBalancer):
    """Uniform random selection, optionally weighted."""

    def select(self) -> Upstream:
        healthy = self.healthy_upstreams
        if not healthy:
            raise RuntimeError("No healthy upstreams available")
        weights = [u.weight for u in healthy]
        total = sum(weights)
        if total == 0:
            return random.choice(healthy)
        r = random.uniform(0, total)
        cumulative = 0.0
        for u, w in zip(healthy, weights):
            cumulative += w
            if r <= cumulative:
                return u
        return healthy[-1]


def create_balancer(
    strategy: str,
    upstreams: list[Upstream],
) -> LoadBalancer:
    """Factory function matching config strategy strings."""
    mapping: dict[str, type[LoadBalancer]] = {
        "round_robin": RoundRobinBalancer,
        "weighted_round_robin": WeightedRoundRobinBalancer,
        "least_connections": LeastConnectionsBalancer,
        "random": RandomBalancer,
    }
    cls = mapping.get(strategy)
    if cls is None:
        raise ValueError(f"Unknown load balancer strategy: {strategy!r}. Choose from: {list(mapping)}")
    return cls(upstreams)
