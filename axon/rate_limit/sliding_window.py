"""
Sliding Window Rate Limiter backed by Redis.

Uses a sorted-set Lua script for atomic O(log N) evaluation.
Each key maps to a sorted set where members are request IDs and
scores are epoch timestamps in milliseconds. Expired entries are
pruned on every call, so memory stays bounded.

Algorithm complexity: O(log N) per request where N ≤ burst size.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

# Atomic Lua script — runs as a single Redis command, no MULTI/EXEC needed.
# KEYS[1] : rate limit key (e.g. "axon:rl:127.0.0.1:/v1/predict")
# ARGV[1] : window size in milliseconds
# ARGV[2] : max requests allowed in the window
# ARGV[3] : current timestamp in milliseconds
# ARGV[4] : unique request ID (member in the sorted set)
#
# Returns: { allowed (0|1), current_count, ttl_ms }
_SLIDING_WINDOW_SCRIPT = """
local key        = KEYS[1]
local window_ms  = tonumber(ARGV[1])
local max_req    = tonumber(ARGV[2])
local now_ms     = tonumber(ARGV[3])
local req_id     = ARGV[4]
local window_start = now_ms - window_ms

-- Remove entries outside the window
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

local count = redis.call('ZCARD', key)

if count < max_req then
    redis.call('ZADD', key, now_ms, req_id)
    redis.call('PEXPIRE', key, window_ms)
    return {1, count + 1, window_ms}
else
    local oldest = tonumber(redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')[2])
    local retry_after_ms = window_ms - (now_ms - oldest)
    return {0, count, retry_after_ms}
end
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    current_count: int
    limit: int
    remaining: int
    retry_after_ms: int  # 0 when allowed


class SlidingWindowRateLimiter:
    """
    Distributed sliding-window rate limiter.

    Thread-safe and process-safe via Redis atomics.
    Multiple gateway instances share state automatically.
    """

    def __init__(
        self,
        redis: Redis,
        requests_per_second: float,
        burst: int,
        key_prefix: str = "axon:rl:",
    ) -> None:
        self._redis = redis
        self._rps = requests_per_second
        self._burst = burst
        self._window_ms = int(1000 / requests_per_second * burst)
        self._prefix = key_prefix
        self._script = redis.register_script(_SLIDING_WINDOW_SCRIPT)

    @property
    def window_ms(self) -> int:
        return self._window_ms

    async def check(self, identifier: str) -> RateLimitResult:
        """
        Evaluate a request against the rate limit.

        Args:
            identifier: Unique per-client key (IP, API key hash, user ID).

        Returns:
            RateLimitResult with allow/deny decision and metadata.
        """
        key = f"{self._prefix}{identifier}"
        now_ms = int(time.time() * 1000)
        req_id = str(uuid.uuid4())

        result = await self._script(
            keys=[key],
            args=[self._window_ms, self._burst, now_ms, req_id],
        )

        allowed, count, extra = int(result[0]), int(result[1]), int(result[2])
        return RateLimitResult(
            allowed=bool(allowed),
            current_count=count,
            limit=self._burst,
            remaining=max(0, self._burst - count),
            retry_after_ms=extra if not allowed else 0,
        )

    async def reset(self, identifier: str) -> None:
        """Delete the rate limit key (useful in tests and admin endpoints)."""
        await self._redis.delete(f"{self._prefix}{identifier}")
