"""
Tests for rate limiting algorithms.

Covers: allow/deny decisions, burst handling, key isolation,
reset functionality, and result metadata correctness.
"""

from __future__ import annotations

import asyncio

import pytest

from axon.rate_limit.sliding_window import SlidingWindowRateLimiter
from axon.rate_limit.token_bucket import TokenBucketRateLimiter


class TestSlidingWindow:
    async def test_allows_under_limit(self, sliding_window: SlidingWindowRateLimiter) -> None:
        for _ in range(5):
            result = await sliding_window.check("user:alice")
            assert result.allowed
            assert result.limit == 10

    async def test_denies_over_burst(self, sliding_window: SlidingWindowRateLimiter) -> None:
        # Exhaust the burst
        for _ in range(10):
            await sliding_window.check("user:burst-test")
        # 11th should be denied
        result = await sliding_window.check("user:burst-test")
        assert not result.allowed
        assert result.remaining == 0
        assert result.retry_after_ms > 0

    async def test_key_isolation(self, sliding_window: SlidingWindowRateLimiter) -> None:
        """Different identifiers must not share quota."""
        for _ in range(10):
            await sliding_window.check("user:alice")
        # alice is exhausted, but bob should still be allowed
        result = await sliding_window.check("user:bob")
        assert result.allowed

    async def test_reset_clears_state(self, sliding_window: SlidingWindowRateLimiter) -> None:
        for _ in range(10):
            await sliding_window.check("user:reset-test")
        deny = await sliding_window.check("user:reset-test")
        assert not deny.allowed

        await sliding_window.reset("user:reset-test")
        result = await sliding_window.check("user:reset-test")
        assert result.allowed

    async def test_result_metadata_consistency(self, sliding_window: SlidingWindowRateLimiter) -> None:
        result = await sliding_window.check("user:meta")
        assert result.allowed
        assert result.current_count >= 1
        assert result.remaining == result.limit - result.current_count

    async def test_remaining_decrements(self, sliding_window: SlidingWindowRateLimiter) -> None:
        r1 = await sliding_window.check("user:decrement")
        r2 = await sliding_window.check("user:decrement")
        assert r2.remaining == r1.remaining - 1


class TestTokenBucket:
    async def test_allows_under_capacity(self, token_bucket: TokenBucketRateLimiter) -> None:
        for _ in range(5):
            result = await token_bucket.check("user:alice")
            assert result.allowed

    async def test_denies_when_exhausted(self, token_bucket: TokenBucketRateLimiter) -> None:
        for _ in range(10):
            await token_bucket.check("user:exhaust")
        result = await token_bucket.check("user:exhaust")
        assert not result.allowed
        assert result.retry_after_ms > 0

    async def test_key_isolation(self, token_bucket: TokenBucketRateLimiter) -> None:
        for _ in range(10):
            await token_bucket.check("user:full")
        result = await token_bucket.check("user:fresh")
        assert result.allowed

    async def test_higher_cost_consumes_more(self, token_bucket: TokenBucketRateLimiter) -> None:
        """Cost=5 should consume 5 tokens in one call."""
        result = await token_bucket.check("user:costly", cost=5)
        assert result.allowed
        assert result.tokens_remaining <= 5  # started at 10, consumed 5

    async def test_reset_refills_bucket(self, token_bucket: TokenBucketRateLimiter) -> None:
        for _ in range(10):
            await token_bucket.check("user:reset")
        denied = await token_bucket.check("user:reset")
        assert not denied.allowed

        await token_bucket.reset("user:reset")
        result = await token_bucket.check("user:reset")
        assert result.allowed

    async def test_capacity_field(self, token_bucket: TokenBucketRateLimiter) -> None:
        result = await token_bucket.check("user:cap")
        assert result.capacity == 10
