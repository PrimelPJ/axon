"""
Tests for authentication modules.

Covers: valid tokens, expired tokens, wrong secrets, missing claims,
API key lookup, expiry, and revocation.
"""

from __future__ import annotations

import time

import jwt
import pytest

from axon.auth.api_key import APIKeyError, APIKeyRegistry
from axon.auth.jwt import JWTError, JWTValidator, TokenClaims
from axon.config import AuthConfig

JWT_SECRET = "test-secret-key-for-axon-unit-tests"


def _make_token(
    subject: str = "user_123",
    secret: str = JWT_SECRET,
    algorithm: str = "HS256",
    exp_offset: int = 3600,
    extra: dict | None = None,
) -> str:
    payload = {
        "sub": subject,
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_offset,
        **(extra or {}),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


class TestJWTValidator:
    @pytest.fixture
    def validator(self) -> JWTValidator:
        return JWTValidator(AuthConfig(jwt_secret=JWT_SECRET, jwt_algorithm="HS256"))

    def test_valid_token_returns_claims(self, validator: JWTValidator) -> None:
        token = _make_token(subject="user_abc")
        claims = validator.validate(token)
        assert isinstance(claims, TokenClaims)
        assert claims.subject == "user_abc"

    def test_expired_token_raises(self, validator: JWTValidator) -> None:
        token = _make_token(exp_offset=-1)
        with pytest.raises(JWTError, match="expired"):
            validator.validate(token)

    def test_wrong_secret_raises(self, validator: JWTValidator) -> None:
        token = _make_token(secret="wrong-secret")
        with pytest.raises(JWTError):
            validator.validate(token)

    def test_missing_sub_raises(self, validator: JWTValidator) -> None:
        payload = {"iat": int(time.time()), "exp": int(time.time()) + 3600}
        token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
        with pytest.raises(JWTError, match="sub"):
            validator.validate(token)

    def test_scopes_parsed_from_space_string(self, validator: JWTValidator) -> None:
        token = _make_token(extra={"scope": "read:models write:predict"})
        claims = validator.validate(token)
        assert claims.has_scope("read:models")
        assert claims.has_scope("write:predict")
        assert not claims.has_scope("admin")

    def test_scopes_parsed_from_list(self, validator: JWTValidator) -> None:
        token = _make_token(extra={"scopes": ["read:models", "admin"]})
        claims = validator.validate(token)
        assert claims.has_scope("admin")

    def test_user_id_falls_back_to_subject(self, validator: JWTValidator) -> None:
        token = _make_token(subject="svc_account")
        claims = validator.validate(token)
        assert claims.user_id == "svc_account"

    def test_user_id_from_uid_claim(self, validator: JWTValidator) -> None:
        token = _make_token(extra={"uid": "custom_uid_999"})
        claims = validator.validate(token)
        assert claims.user_id == "custom_uid_999"

    def test_malformed_token_raises(self, validator: JWTValidator) -> None:
        with pytest.raises(JWTError):
            validator.validate("not.a.token")


class TestAPIKeyRegistry:
    async def test_register_and_validate(self, api_key_registry: APIKeyRegistry) -> None:
        raw_key = "axon_test_abc123def456"
        await api_key_registry.register(raw_key, owner="team-ml", scopes=["read:*"])
        meta = await api_key_registry.validate(raw_key)
        assert meta.owner == "team-ml"
        assert "read:*" in meta.scopes

    async def test_validate_unknown_key_raises(self, api_key_registry: APIKeyRegistry) -> None:
        with pytest.raises(APIKeyError, match="not found"):
            await api_key_registry.validate("axon_test_nonexistent_key")

    async def test_validate_expired_key_raises(self, api_key_registry: APIKeyRegistry) -> None:
        raw_key = "axon_test_expiredkey"
        await api_key_registry.register(
            raw_key,
            owner="expired-svc",
            scopes=[],
            expires_at=time.time() - 1,  # already expired
        )
        with pytest.raises(APIKeyError, match="expired"):
            await api_key_registry.validate(raw_key)

    async def test_revoke_invalidates_key(self, api_key_registry: APIKeyRegistry) -> None:
        raw_key = "axon_test_revoketest"
        await api_key_registry.register(raw_key, owner="svc", scopes=[])
        await api_key_registry.revoke(raw_key)
        with pytest.raises(APIKeyError):
            await api_key_registry.validate(raw_key)

    async def test_revoke_nonexistent_returns_false(self, api_key_registry: APIKeyRegistry) -> None:
        result = await api_key_registry.revoke("axon_test_doesnotexist")
        assert result is False

    async def test_rate_limit_override_preserved(self, api_key_registry: APIKeyRegistry) -> None:
        raw_key = "axon_test_ratelimit"
        await api_key_registry.register(raw_key, owner="premium", scopes=[], rate_limit_override=5000)
        meta = await api_key_registry.validate(raw_key)
        assert meta.rate_limit_override == 5000

    def test_generate_key_format(self) -> None:
        key = APIKeyRegistry.generate_key("prod")
        assert key.startswith("axon_prod_")
        assert len(key) == len("axon_prod_") + 32

    def test_hash_is_deterministic(self) -> None:
        raw = "axon_test_hashme"
        h1 = APIKeyRegistry.hash_key(raw)
        h2 = APIKeyRegistry.hash_key(raw)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_hash_is_different_for_different_keys(self) -> None:
        assert APIKeyRegistry.hash_key("key_a") != APIKeyRegistry.hash_key("key_b")
