"""
Authentication Middleware

Extracts credentials from the request and authenticates via JWT or API key.
Attaches the resolved identity to request.state for downstream use.

Precedence:  Authorization: Bearer <token>  >  X-API-Key: <key>

Paths listed in `public_paths` bypass authentication entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from axon.auth.api_key import APIKeyError, APIKeyMetadata, APIKeyRegistry
from axon.auth.jwt import JWTError, JWTValidator, TokenClaims
from axon.config import get_settings
from axon.observability.logging import get_logger
from axon.observability.metrics import AUTH_TOTAL

logger = get_logger(__name__)

# These paths always bypass auth, regardless of route configuration
_ALWAYS_PUBLIC: frozenset[str] = frozenset({
    "/health",
    "/ready",
    "/metrics",
    "/_axon/status",
})


@dataclass
class Identity:
    """Resolved identity attached to request.state after successful auth."""
    auth_method: str  # "jwt" | "api_key"
    subject: str
    owner: str
    scopes: list[str]
    rate_limit_override: int | None = None


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Auth middleware. Runs before route matching.

    Attaches `request.state.identity` on success.
    Returns 401 on missing credentials, 403 on invalid credentials.
    """

    def __init__(
        self,
        app: Callable,
        jwt_validator: JWTValidator | None,
        api_key_registry: APIKeyRegistry | None,
    ) -> None:
        super().__init__(app)
        self._jwt = jwt_validator
        self._api_keys = api_key_registry

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Always-public paths
        if path in _ALWAYS_PUBLIC or path.startswith("/_axon/"):
            return await call_next(request)

        # Check if this route requires auth
        settings = get_settings()
        route_cfg = None
        for route in settings.routes:
            if path == route.path or path.startswith(route.path.rstrip("/") + "/"):
                route_cfg = route
                break

        if route_cfg is not None and not route_cfg.require_auth:
            return await call_next(request)

        # ── Try JWT ─────────────────────────────────────────────────────────
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and self._jwt:
            token = auth_header.removeprefix("Bearer ").strip()
            try:
                claims: TokenClaims = self._jwt.validate(token)
                request.state.identity = Identity(
                    auth_method="jwt",
                    subject=claims.subject,
                    owner=claims.user_id,
                    scopes=claims.scopes,
                )
                AUTH_TOTAL.labels(method="jwt", result="success").inc()
                return await call_next(request)
            except JWTError as exc:
                AUTH_TOTAL.labels(method="jwt", result="failure").inc()
                logger.info("jwt_auth_failed", reason=str(exc), path=path)
                return _unauthorized(f"JWT validation failed: {exc}")

        # ── Try API key ──────────────────────────────────────────────────────
        settings = get_settings()
        api_key = request.headers.get(settings.auth.api_key_header, "")
        if api_key and self._api_keys:
            try:
                meta: APIKeyMetadata = await self._api_keys.validate(api_key)
                request.state.identity = Identity(
                    auth_method="api_key",
                    subject=meta.key_id,
                    owner=meta.owner,
                    scopes=meta.scopes,
                    rate_limit_override=meta.rate_limit_override,
                )
                AUTH_TOTAL.labels(method="api_key", result="success").inc()
                return await call_next(request)
            except APIKeyError as exc:
                AUTH_TOTAL.labels(method="api_key", result="failure").inc()
                logger.info("api_key_auth_failed", reason=str(exc), path=path)
                return _forbidden(f"API key invalid: {exc}")

        # No credentials provided
        return _unauthorized("authentication required — provide Bearer token or API key")


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": "unauthorized", "detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"error": "forbidden", "detail": detail},
    )
