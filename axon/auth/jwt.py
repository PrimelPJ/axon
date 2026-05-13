"""
JWT Authentication

Validates Bearer tokens in the Authorization header.
Supports both symmetric (HS256) and asymmetric (RS256/RS512) algorithms.

For ML infrastructure deployments, service-to-service calls typically use
short-lived RS256 JWTs signed by an internal CA or identity provider (e.g.
AWS Cognito, Auth0, or a custom token service).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from jwt.exceptions import DecodeError, ExpiredSignatureError, InvalidTokenError

from axon.config import AuthConfig
from axon.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: str
    issuer: str | None
    audience: str | list[str] | None
    expires_at: float
    issued_at: float
    raw: dict[str, Any]

    @property
    def user_id(self) -> str:
        return self.raw.get("user_id") or self.raw.get("uid") or self.subject

    @property
    def scopes(self) -> list[str]:
        scope = self.raw.get("scope") or self.raw.get("scopes", "")
        if isinstance(scope, list):
            return scope
        return scope.split() if scope else []

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class JWTError(Exception):
    """Raised when token validation fails. Message is safe to return to client."""


class JWTValidator:
    """
    Validates and decodes JWTs.

    Configure once at startup and reuse across requests. The public key (for
    RS256) is loaded from disk once during __init__ and cached in memory.
    """

    def __init__(self, config: AuthConfig) -> None:
        self._algorithm = config.jwt_algorithm
        self._leeway = config.jwt_expiry_leeway_seconds
        self._issuer = config.jwt_issuer
        self._audience = config.jwt_audience

        if self._algorithm in ("RS256", "RS384", "RS512"):
            if not config.jwt_public_key_path:
                raise ValueError(f"jwt_public_key_path required for {self._algorithm}")
            key_path = Path(config.jwt_public_key_path)
            if not key_path.exists():
                raise FileNotFoundError(f"JWT public key not found: {key_path}")
            self._key: str | bytes = key_path.read_text()
        else:
            if not config.jwt_secret:
                raise ValueError("jwt_secret required for HS256")
            self._key = config.jwt_secret

    def validate(self, token: str) -> TokenClaims:
        """
        Validate the token and return its claims.

        Raises:
            JWTError: Token is invalid, expired, or has wrong issuer/audience.
        """
        options: dict[str, Any] = {
            "verify_signature": True,
            "verify_exp": True,
            "verify_iat": True,
            "leeway": self._leeway,
        }

        decode_kwargs: dict[str, Any] = {
            "key": self._key,
            "algorithms": [self._algorithm],
            "options": options,
        }

        if self._audience:
            decode_kwargs["audience"] = self._audience
        if self._issuer:
            decode_kwargs["issuer"] = self._issuer

        try:
            payload: dict[str, Any] = jwt.decode(token, **decode_kwargs)
        except ExpiredSignatureError:
            raise JWTError("token expired")
        except DecodeError as exc:
            raise JWTError(f"token decode error: {exc}")
        except InvalidTokenError as exc:
            raise JWTError(f"invalid token: {exc}")

        subject = payload.get("sub")
        if not subject:
            raise JWTError("missing 'sub' claim")

        return TokenClaims(
            subject=str(subject),
            issuer=payload.get("iss"),
            audience=payload.get("aud"),
            expires_at=float(payload.get("exp", 0)),
            issued_at=float(payload.get("iat", 0)),
            raw=payload,
        )

    def decode_unverified_header(self, token: str) -> dict[str, Any]:
        """Read the header without signature verification (for debugging only)."""
        return jwt.get_unverified_header(token)
