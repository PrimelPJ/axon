"""
API Key Authentication

API keys are never stored in plaintext. The registry stores SHA-256 hashes
and the gateway computes the hash of the incoming key to look up metadata.

Key format:  axon_{env}_{random_32_hex}
Example:     axon_prod_a1b2c3d4e5f6...

In production, key metadata (owner, scopes, expiry) lives in Redis.
Redis key schema:   axon:apikey:{sha256_hex}  →  JSON metadata string
TTL matches key expiry so expired keys auto-purge from Redis.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

from axon.observability.logging import get_logger

logger = get_logger(__name__)

_KEY_PREFIX_LENGTH = 8  # Characters of raw key shown in logs ("axon_pro")


@dataclass
class APIKeyMetadata:
    key_id: str
    owner: str
    scopes: list[str]
    created_at: float
    expires_at: float | None  # None = never expires
    rate_limit_override: int | None  # req/s override, None = use global

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def to_redis(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_redis(cls, raw: str) -> "APIKeyMetadata":
        data = json.loads(raw)
        return cls(**data)


class APIKeyError(Exception):
    """Raised when API key validation fails."""


class APIKeyRegistry:
    """
    Redis-backed API key registry.

    Lookups are O(1) via Redis GET on the hashed key.
    """

    def __init__(self, redis: Redis, key_prefix: str = "axon:apikey:") -> None:
        self._redis = redis
        self._prefix = key_prefix

    @staticmethod
    def hash_key(raw_key: str) -> str:
        """Deterministically hash an API key for storage/lookup."""
        return hashlib.sha256(raw_key.encode()).hexdigest()

    async def validate(self, raw_key: str) -> APIKeyMetadata:
        """
        Look up the key hash in Redis and return metadata.

        Raises:
            APIKeyError: Key not found, expired, or malformed.
        """
        if not raw_key:
            raise APIKeyError("empty api key")

        key_hash = self.hash_key(raw_key)
        redis_key = f"{self._prefix}{key_hash}"

        raw_meta = await self._redis.get(redis_key)
        if raw_meta is None:
            # Log safe prefix only — never log the full key
            logger.warning("api_key_not_found", key_prefix=raw_key[:_KEY_PREFIX_LENGTH])
            raise APIKeyError("api key not found")

        try:
            meta = APIKeyMetadata.from_redis(
                raw_meta if isinstance(raw_meta, str) else raw_meta.decode()
            )
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.error("api_key_metadata_corrupt", key_prefix=raw_key[:_KEY_PREFIX_LENGTH], error=str(exc))
            raise APIKeyError("api key metadata corrupt") from exc

        if meta.is_expired:
            logger.info("api_key_expired", key_id=meta.key_id, owner=meta.owner)
            raise APIKeyError("api key expired")

        return meta

    async def register(
        self,
        raw_key: str,
        owner: str,
        scopes: list[str],
        expires_at: float | None = None,
        rate_limit_override: int | None = None,
    ) -> APIKeyMetadata:
        """
        Register a new API key. Idempotent — registering the same key twice
        is safe (overwrites metadata).
        """
        key_hash = self.hash_key(raw_key)
        key_id = key_hash[:16]
        meta = APIKeyMetadata(
            key_id=key_id,
            owner=owner,
            scopes=scopes,
            created_at=time.time(),
            expires_at=expires_at,
            rate_limit_override=rate_limit_override,
        )

        redis_key = f"{self._prefix}{key_hash}"
        ttl: int | None = None
        if expires_at:
            ttl = max(1, int(expires_at - time.time()))

        if ttl:
            await self._redis.set(redis_key, meta.to_redis(), ex=ttl)
        else:
            await self._redis.set(redis_key, meta.to_redis())

        logger.info("api_key_registered", key_id=key_id, owner=owner, scopes=scopes)
        return meta

    async def revoke(self, raw_key: str) -> bool:
        """Immediately invalidate a key. Returns True if the key existed."""
        key_hash = self.hash_key(raw_key)
        deleted = await self._redis.delete(f"{self._prefix}{key_hash}")
        return bool(deleted)

    @staticmethod
    def generate_key(env: str = "prod") -> str:
        """
        Generate a new API key. Must be stored by caller — never recoverable.
        Format: axon_{env}_{32 hex chars}
        """
        random_part = os.urandom(16).hex()
        return f"axon_{env}_{random_part}"
