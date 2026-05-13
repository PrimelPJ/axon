# Axon Gateway

A production-grade ML-native API gateway written in Python. Handles authentication,
adaptive routing, rate limiting, circuit breaking, and full observability for
machine learning inference infrastructure.

Built to the operational requirements of high-throughput ML serving systems —
think the layer in front of your model servers that lets you ship faster and sleep better.

---

## Architecture

```
                         ┌─────────────────────────────────────────────┐
                         │              Axon Gateway                   │
                         │                                             │
 Client ──HTTP──▶        │  ObservabilityMiddleware  (request ID, log) │
                         │         ↓                                   │
                         │  AuthMiddleware            (JWT / API key)  │
                         │         ↓                                   │
                         │  RateLimitMiddleware       (sliding window) │
                         │         ↓                                   │
                         │  RouteRegistry             (prefix match)   │
                         │         ↓                                   │
                         │  LoadBalancer              (WRR / LC / RR)  │
                         │         ↓                                   │
                         │  CircuitBreaker            (Redis-backed)   │
                         │         ↓                                   │
                         │  ReverseProxy              (httpx async)    │
                         └──────────────┬──────────────────────────────┘
                                        │
                   ┌────────────────────┼────────────────────┐
                   ▼                    ▼                     ▼
           model-v2:8000       model-v3-canary:8000   batch-worker:8001
           (weight: 9)         (weight: 1)            (least-connections)
```

State (rate limit counters, circuit breaker state, API keys) is stored in Redis,
so multiple gateway replicas share a consistent view automatically.

---

## Features

**Authentication**
- JWT validation — HS256 and RS256/RS512, configurable issuer/audience checks
- API key authentication — SHA-256 hashed, stored in Redis with TTL
- Per-route `require_auth` toggle for public endpoints
- Auth identity flows downstream for rate limit key selection

**Routing**
- Longest-prefix path matching (consistent with Nginx/Envoy behaviour)
- Four load balancing strategies: round-robin, weighted round-robin (smooth Nginx algorithm), least-connections, random
- Async reverse proxy with configurable retry + exponential backoff
- `strip_prefix` support for path rewriting

**Rate Limiting**
- Two algorithms: sliding window (sorted-set Lua) and token bucket (atomic Lua)
- Atomic Redis Lua scripts — correct under concurrent multi-replica access
- Key strategies: `ip`, `api_key`, `user_id`
- Per-route overrides and per-API-key personal limits
- Standard `X-RateLimit-*` response headers

**Circuit Breaker**
- Classic three-state machine: closed → open → half-open → closed
- State stored in Redis — shared across all gateway replicas
- Configurable failure threshold, recovery timeout, and half-open probe count
- Per-route circuit breaker configuration

**Observability**
- Structured JSON logging via `structlog` with request context propagation
- 15+ Prometheus metrics: request counts, latency histograms, rate limit decisions, circuit breaker transitions, upstream errors
- OpenTelemetry tracing with OTLP export (Jaeger, Grafana Tempo, etc.)
- Unique request ID on every request (`X-Request-ID` header)
- Slow request detection and logging

---

## Quickstart

### Docker Compose (recommended)

```bash
git clone https://github.com/PrimelPJ/axon.git
cd axon
export JWT_SECRET="your-secret-here"
docker compose -f docker/docker-compose.yml up
```

This starts:
- Axon Gateway on `:8080`
- Redis on `:6379`
- Example ML backend on `:8000`
- Prometheus on `:9090`
- Grafana on `:3000` (admin / axon)

### Local Development

```bash
git clone https://github.com/PrimelPJ/axon.git
cd axon
pip install -e ".[dev]"

# Start Redis (or use Docker)
docker run -d -p 6379:6379 redis:7.4-alpine

# Run with example config
AXON_AUTH__JWT_SECRET="dev-secret" \
AXON_DEBUG=true \
uvicorn axon.main:app --reload --port 8080
```

### Configuration

All settings can be provided via `config.yaml` (path set with `AXON_CONFIG_FILE`),
environment variables (`AXON_` prefix), or a `.env` file.

```yaml
# config.yaml
auth:
  jwt_algorithm: HS256
  jwt_secret: ""   # use AXON_AUTH__JWT_SECRET env var

rate_limit:
  enabled: true
  algorithm: sliding_window
  requests_per_second: 100
  burst: 200

routes:
  - path: /v1/predict
    require_auth: true
    load_balancer: weighted_round_robin
    timeout_seconds: 30
    upstreams:
      - url: http://model-v2:8000
        weight: 9
      - url: http://model-v3:8000
        weight: 1
```

See `config.yaml` for the full reference configuration.

---

## API Reference

### System Endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `GET /health` | No | Liveness probe |
| `GET /ready` | No | Readiness probe (checks Redis) |
| `GET /metrics` | No | Prometheus metrics |
| `GET /_axon/status` | No | Route registry status |

### Authentication

**JWT**
```
Authorization: Bearer <token>
```

**API Key**
```
X-API-Key: axon_prod_<32hex>
```

### Rate Limit Headers

Every proxied response includes:
```
X-RateLimit-Limit: 200
X-RateLimit-Remaining: 197
```

When rate limited (HTTP 429):
```json
{
  "error": "rate_limit_exceeded",
  "detail": "Too many requests. Retry after 1s.",
  "retry_after_seconds": 1
}
```

---

## Testing

```bash
# Run all tests with coverage
pytest

# Run a specific module
pytest tests/test_rate_limit.py -v

# Run with live Redis (integration)
AXON_REDIS__URL=redis://localhost:6379/15 pytest
```

Tests use `fakeredis` for Redis and `respx` for HTTP mocking — no live services required by default.

---

## Project Structure

```
axon/
├── axon/
│   ├── auth/
│   │   ├── api_key.py        # API key hashing + Redis registry
│   │   └── jwt.py            # JWT validation (HS256 / RS256)
│   ├── middleware/
│   │   ├── auth.py           # Auth middleware
│   │   ├── observability.py  # Logging + metrics middleware
│   │   └── rate_limit.py     # Rate limiting middleware
│   ├── observability/
│   │   ├── logging.py        # structlog configuration
│   │   ├── metrics.py        # Prometheus metric definitions
│   │   └── tracing.py        # OpenTelemetry setup
│   ├── rate_limit/
│   │   ├── sliding_window.py # Sorted-set Lua algorithm
│   │   └── token_bucket.py   # Token bucket Lua algorithm
│   ├── routing/
│   │   ├── circuit_breaker.py # 3-state Redis-backed CB
│   │   ├── load_balancer.py   # RR, WRR, LC, Random
│   │   ├── proxy.py           # Async reverse proxy + retries
│   │   └── registry.py        # Route registry + prefix matching
│   ├── config.py             # Pydantic Settings
│   └── main.py               # FastAPI app factory + lifespan
├── tests/
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_rate_limit.py
│   └── test_routing.py
├── docker/
│   ├── Dockerfile            # Multi-stage production build
│   ├── docker-compose.yml    # Full stack (gateway + Redis + Prometheus + Grafana)
│   └── prometheus.yml
├── config.yaml               # Reference configuration
└── pyproject.toml
```

---

## Design Decisions

**Why Lua scripts for rate limiting?**
Redis Lua scripts execute atomically — no race conditions between read-modify-write
operations, even with dozens of gateway replicas. The alternative (WATCH/MULTI/EXEC)
adds round trips and retry complexity.

**Why store circuit breaker state in Redis?**
A per-process circuit breaker means Replica A might think a service is healthy while
Replica B is OPEN, routing 50% of traffic to a dead upstream. Shared state in Redis
gives a consistent cluster-wide view.

**Why Starlette middleware instead of FastAPI dependencies?**
Middleware runs unconditionally on every request including 404s and OPTIONS preflight.
FastAPI dependencies only run on matched routes — incorrect for auth and rate limiting
which must protect the catch-all proxy route.

**Why `httpx.AsyncClient` with a shared connection pool?**
A single client instance reuses TCP connections across requests (keepalive pool).
Creating a new client per-request would open a new TCP+TLS handshake for every
upstream call — catastrophic for latency under load.

---

## License

MIT — see [LICENSE](LICENSE).
