"""
Token Bucket Rate Limiter backed by Redis.

Classic leaky-bucket variant: tokens replenish continuously at a fixed rate.
Supports burst capacity — a client can consume up to `burst` tokens instantly
and then is throttled to `requests_per_second`.

Implemented as an atomic Lua script to guarantee correctness under concurrent
access from multiple gateway instances.

Time complexity: O(1) per request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis

# KEYS[1] : token count key  (float stored as string)
# KEYS[2] : last refill timestamp key (float stored as string)
# ARGV[1] : bucket capacity (burst)
# ARGV[2] : refill rate (tokens per second, float)
# ARGV[3] : current time (float seconds since epoch)
# ARGV[4] : cost per request (usually 1)
#
# Returns: { allowed (0|1), tokens_remaining_floor, ttl_seconds }
_TOKEN_BUCKET_SCRIPT = """
local tokens_key     = KEYS[1]
local timestamp_key  = KEYS[2]
local capacity       = tonumber(ARGV[1])
local refill_rate    = tonumber(ARGV[2])
local now            = tonumber(ARGV[3])
local cost           = tonumber(ARGV[4])

local last_tokens    = tonumber(redis.call('GET', tokens_key))
local last_refill    = tonumber(redis.call('GET', timestamp_key))

if last_tokens == nil then
    last_tokens = capacity
end
if last_refill == nil then
    last_refill = now
end

-- Refill tokens proportional to elapsed time
local elapsed = math.max(0, now - last_refill)
local new_tokens = math.min(capacity, last_tokens + elapsed * refill_rate)

if new_tokens >= cost then
    new_tokens = new_tokens - cost
    local ttl = math.ceil(capacity / refill_rate)
    redis.call('SET', tokens_key, new_tokens, 'EX', ttl)
    redis.call('SET', timestamp_key, now, 'EX', ttl)
    return {1, math.floor(new_tokens), ttl}
else
    -- Not enough tokens — calculate wait time
    local deficit = cost - new_tokens
    local wait_ms = math.ceil((deficit / refill_rate) * 1000)
    redis.call('SET', tokens_key, new_tokens, 'EX', math.ceil(capacity / refill_rate))
    redis.call('SET', timestamp_key, now, 'EX', math.ceil(capacity / refill_rate))
    return {0, math.floor(new_tokens), wait_ms}
end
"""


@dataclass(frozen=True, slots=True)
class TokenBucketResult:
    allowed: bool
    tokens_remaining: int
    capacity: int
    retry_after_ms: int  # 0 when allowed


class TokenBucketRateLimiter:
    """
    Distributed token bucket rate limiter.

    Allows burst up to `capacity`, then throttles to `requests_per_second`.
    State is shared across gateway replicas via Redis.
    """

    def __init__(
        self,
        redis: Redis,
        requests_per_second: float,
        burst: int,
        key_prefix: str = "axon:tb:",
    ) -> None:
        self._redis = redis
        self._rps = requests_per_second
        self._burst = burst
        self._prefix = key_prefix
        self._script = redis.register_script(_TOKEN_BUCKET_SCRIPT)

    async def check(self, identifier: str, cost: int = 1) -> TokenBucketResult:
        """
        Attempt to consume `cost` tokens from the bucket.

        Args:
            identifier: Per-client key.
            cost: Token cost (default 1; use higher values for expensive endpoints).

        Returns:
            TokenBucketResult indicating whether the request is allowed.
        """
        tokens_key = f"{self._prefix}tokens:{identifier}"
        ts_key = f"{self._prefix}ts:{identifier}"
        now = time.time()

        result = await self._script(
            keys=[tokens_key, ts_key],
            args=[self._burst, self._rps, now, cost],
        )

        allowed, remaining, extra = int(result[0]), int(result[1]), int(result[2])
        return TokenBucketResult(
            allowed=bool(allowed),
            tokens_remaining=remaining,
            capacity=self._burst,
            retry_after_ms=extra if not allowed else 0,
        )

    async def reset(self, identifier: str) -> None:
        """Flush all bucket state for this identifier."""
        await self._redis.delete(
            f"{self._prefix}tokens:{identifier}",
            f"{self._prefix}ts:{identifier}",
        )
