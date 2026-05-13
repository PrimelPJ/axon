"""
Axon Gateway — Configuration

All settings can be overridden via environment variables (AXON_ prefix)
or via a YAML config file (--config flag or AXON_CONFIG_FILE env var).
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class RateLimitAlgorithm(StrEnum):
    TOKEN_BUCKET = "token_bucket"
    SLIDING_WINDOW = "sliding_window"
    FIXED_WINDOW = "fixed_window"


class LoadBalancerStrategy(StrEnum):
    ROUND_ROBIN = "round_robin"
    WEIGHTED_ROUND_ROBIN = "weighted_round_robin"
    LEAST_CONNECTIONS = "least_connections"
    RANDOM = "random"


class UpstreamConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    url: str
    weight: int = 1
    max_connections: int = 100
    timeout_seconds: float = 30.0
    health_check_path: str = "/health"
    health_check_interval_seconds: float = 10.0
    tags: list[str] = Field(default_factory=list)


class RouteConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    path: str
    upstreams: list[UpstreamConfig]
    strip_prefix: bool = False
    load_balancer: LoadBalancerStrategy = LoadBalancerStrategy.ROUND_ROBIN
    timeout_seconds: float = 30.0
    retries: int = 2
    retry_on: list[int] = Field(default_factory=lambda: [502, 503, 504])
    require_auth: bool = True
    rate_limit_override: RateLimitConfig | None = None
    allowed_methods: list[str] = Field(default_factory=lambda: ["GET", "POST", "PUT", "DELETE", "PATCH"])
    circuit_breaker: CircuitBreakerConfig | None = None
    tags: list[str] = Field(default_factory=list)


class RateLimitConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    enabled: bool = True
    algorithm: RateLimitAlgorithm = RateLimitAlgorithm.SLIDING_WINDOW
    requests_per_second: float = 100.0
    burst: int = 200
    key_func: str = "ip"  # ip | api_key | user_id


class CircuitBreakerConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    enabled: bool = True
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    half_open_max_calls: int = 3
    expected_exception_codes: list[int] = Field(default_factory=lambda: [500, 502, 503, 504])


class AuthConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    jwt_algorithm: str = "HS256"
    jwt_secret: str = ""
    jwt_public_key_path: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_expiry_leeway_seconds: int = 0
    api_key_header: str = "X-API-Key"
    api_key_hash_algorithm: str = "sha256"


class RedisConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    url: str = "redis://localhost:6379/0"
    max_connections: int = 20
    socket_timeout_seconds: float = 1.0
    key_prefix: str = "axon:"


class ObservabilityConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    tracing_enabled: bool = False
    otlp_endpoint: str = "http://localhost:4317"
    service_name: str = "axon-gateway"
    log_level: LogLevel = LogLevel.INFO
    log_format: str = "json"  # json | console
    request_id_header: str = "X-Request-ID"
    slow_request_threshold_ms: float = 1000.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AXON_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8080
    workers: int = 1
    debug: bool = False
    config_file: str | None = Field(default=None, alias="config_file")

    # Sub-configs
    auth: AuthConfig = Field(default_factory=AuthConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    # Routes loaded from config file
    routes: list[RouteConfig] = Field(default_factory=list)

    @field_validator("auth", mode="before")
    @classmethod
    def validate_auth(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return AuthConfig(**v)
        return v

    @model_validator(mode="after")
    def load_config_file(self) -> "Settings":
        config_path = self.config_file or os.environ.get("AXON_CONFIG_FILE")
        if config_path and Path(config_path).exists():
            with open(config_path) as f:
                data = yaml.safe_load(f)
            if routes := data.get("routes"):
                self.routes = [RouteConfig(**r) for r in routes]
            if auth := data.get("auth"):
                self.auth = AuthConfig(**auth)
            if redis := data.get("redis"):
                self.redis = RedisConfig(**redis)
            if rl := data.get("rate_limit"):
                self.rate_limit = RateLimitConfig(**rl)
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def override_settings(new_settings: Settings) -> None:
    """Used in tests to inject mock settings."""
    global _settings
    _settings = new_settings
