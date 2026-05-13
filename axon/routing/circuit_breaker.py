"""
Circuit Breaker — protects upstream services from cascading failures.

State machine:
    CLOSED  ──(failures ≥ threshold)──▶  OPEN
    OPEN    ──(recovery timeout)──────▶  HALF_OPEN
    HALF_OPEN ──(probe succeeds)───────▶  CLOSED
    HALF_OPEN ──(probe fails)──────────▶  OPEN

State is stored in Redis so all gateway replicas share the same view.
This prevents a split-brain where one replica thinks a service is healthy
while another is OPEN, resulting in uneven load distribution.
"""

from __future__ import annotations

import time
from enum import IntEnum
from typing import Any

from redis.asyncio import Redis

from axon.observability.logging import get_logger
from axon.observability.metrics import CIRCUIT_BREAKER_STATE, CIRCUIT_BREAKER_TRANSITIONS

logger = get_logger(__name__)


class CBState(IntEnum):
    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


_STATE_LABELS = {CBState.CLOSED: "closed", CBState.OPEN: "open", CBState.HALF_OPEN: "half_open"}


class CircuitBreaker:
    """
    Redis-backed distributed circuit breaker.

    Each upstream+route pair gets an independent breaker instance.
    """

    def __init__(
        self,
        redis: Redis,
        route: str,
        upstream: str,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        half_open_max_calls: int = 3,
        expected_exception_codes: list[int] | None = None,
        key_prefix: str = "axon:cb:",
    ) -> None:
        self._redis = redis
        self._route = route
        self._upstream = upstream
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_seconds
        self._half_open_max = half_open_max_calls
        self._bad_codes = set(expected_exception_codes or [500, 502, 503, 504])
        self._prefix = key_prefix
        self._key = f"{key_prefix}{route}:{upstream}"

    # ─── State keys ────────────────────────────────────────────────────────────

    @property
    def _state_key(self) -> str:
        return f"{self._key}:state"

    @property
    def _failure_key(self) -> str:
        return f"{self._key}:failures"

    @property
    def _opened_at_key(self) -> str:
        return f"{self._key}:opened_at"

    @property
    def _half_open_calls_key(self) -> str:
        return f"{self._key}:half_calls"

    # ─── Public API ────────────────────────────────────────────────────────────

    async def is_open(self) -> bool:
        """Returns True if the circuit should reject the request."""
        state = await self._get_state()
        if state == CBState.OPEN:
            opened_at = await self._redis.get(self._opened_at_key)
            if opened_at and (time.time() - float(opened_at)) >= self._recovery_timeout:
                await self._transition(CBState.OPEN, CBState.HALF_OPEN)
                return False
            return True
        if state == CBState.HALF_OPEN:
            calls = await self._redis.incr(self._half_open_calls_key)
            if calls > self._half_open_max:
                # Too many probes; back off
                return True
        return False

    async def record_success(self) -> None:
        """Call after a successful upstream response."""
        state = await self._get_state()
        if state == CBState.HALF_OPEN:
            await self._transition(CBState.HALF_OPEN, CBState.CLOSED)
        elif state == CBState.CLOSED:
            await self._redis.delete(self._failure_key)

    async def record_failure(self, status_code: int | None = None) -> None:
        """Call after an upstream error or a bad status code."""
        if status_code is not None and status_code not in self._bad_codes:
            return

        state = await self._get_state()
        if state == CBState.HALF_OPEN:
            await self._transition(CBState.HALF_OPEN, CBState.OPEN)
            return

        failures = await self._redis.incr(self._failure_key)
        await self._redis.expire(self._failure_key, int(self._recovery_timeout * 2))

        if failures >= self._failure_threshold:
            await self._transition(CBState.CLOSED, CBState.OPEN)

    async def reset(self) -> None:
        """Manually close the circuit (e.g. via admin endpoint)."""
        await self._transition(await self._get_state(), CBState.CLOSED)

    async def get_status(self) -> dict[str, Any]:
        state = await self._get_state()
        failures_raw = await self._redis.get(self._failure_key)
        opened_raw = await self._redis.get(self._opened_at_key)
        return {
            "state": _STATE_LABELS[state],
            "failures": int(failures_raw) if failures_raw else 0,
            "failure_threshold": self._failure_threshold,
            "opened_at": float(opened_raw) if opened_raw else None,
            "recovery_timeout_seconds": self._recovery_timeout,
        }

    # ─── Internal helpers ──────────────────────────────────────────────────────

    async def _get_state(self) -> CBState:
        raw = await self._redis.get(self._state_key)
        if raw is None:
            return CBState.CLOSED
        return CBState(int(raw))

    async def _transition(self, from_state: CBState, to_state: CBState) -> None:
        pipe = self._redis.pipeline()

        pipe.set(self._state_key, int(to_state), ex=int(self._recovery_timeout * 10))

        if to_state == CBState.OPEN:
            pipe.set(self._opened_at_key, time.time(), ex=int(self._recovery_timeout * 4))
        elif to_state == CBState.CLOSED:
            pipe.delete(self._failure_key)
            pipe.delete(self._opened_at_key)
            pipe.delete(self._half_open_calls_key)
        elif to_state == CBState.HALF_OPEN:
            pipe.set(self._half_open_calls_key, 0, ex=int(self._recovery_timeout))

        await pipe.execute()

        CIRCUIT_BREAKER_STATE.labels(
            route=self._route, upstream=self._upstream
        ).set(int(to_state))

        CIRCUIT_BREAKER_TRANSITIONS.labels(
            route=self._route,
            upstream=self._upstream,
            from_state=_STATE_LABELS[from_state],
            to_state=_STATE_LABELS[to_state],
        ).inc()

        logger.info(
            "circuit_breaker_transition",
            route=self._route,
            upstream=self._upstream,
            from_state=_STATE_LABELS[from_state],
            to_state=_STATE_LABELS[to_state],
        )
