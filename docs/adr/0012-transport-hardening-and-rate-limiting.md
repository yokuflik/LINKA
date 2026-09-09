# ADR 0012 — Transport hardening and rate limiting

Status: Accepted
Date: 2026-09-06

Full step-by-step delivery plan: `COMMS_SECURITY_PLAN.md` (root).

## Context

The PoC is now internet-reachable (ADR 0007, live on the sslip.io host) with a
real phone-auth flow (ADR 0009) and content-addressed media (ADR 0010). Nothing
yet defends the abusable surfaces:

- **Bot signup / OTP spray** — `POST /auth/otp/request` is capped only
  per-phone (20 / 10 min) and not at all per-IP, so one host can spray OTPs at
  thousands of numbers.
- **Credential / OTP brute force** — verify endpoints lean on the OTP TTL cap
  only.
- **WS connection-exhaustion DoS** — no cap on concurrent WebSocket
  connections per user; each connect runs a `get_all_chat_ids_for_user` DB
  query and Redis routing writes.
- **Message / search spam** — `send_message` is a single fixed-window
  `INCR`/`EXPIRE` (20 / 10 s) that lets a ~2× boundary burst through.
- **History-scrape DB load** — `GET /chats/{id}/messages` has an unbounded
  `limit` and no per-user cap.
- **Storage / bandwidth abuse** — upload-ticket endpoint is unthrottled.
- **Transport** — no HSTS / security headers / `TrustedHost`; CORS is
  `allow_origins=["*"]` **with** `allow_credentials=True` (invalid, silently
  unsafe); `/ws` does not check `Origin`.

Message-content encryption at rest and semantic search are **explicitly out of
scope** — separate ADRs with their own threat models (see COMMS_SECURITY_PLAN
"Deliberately deferred").

## Decision

### Layering

- **Caddy owns** TLS, security headers, request-body ceilings, and coarse
  per-IP rate ceilings (a backstop, not the primary control).
- **The app owns** every per-user / per-identity limit, in Redis.

No in-process-only limiter: the app is designed to run multiple workers /
processes (fan-out, routing), so any counter must be shared → Redis. The demo
runs one worker today, but the limiter must not assume it.

### Rate-limiter engine

`services/rate_limit_service.py` keeps the existing fixed-window
`check_and_increment` for coarse limits and gains:

- `check_sliding_window(identifier, action, max_per_window, window_seconds)` —
  a Redis sorted-set log driven by one cached Lua script (`EVALSHA`): drop
  entries older than `now - window`, `ZCARD`, conditional `ZADD` + `PEXPIRE`,
  all atomic. Used wherever a 2× boundary burst matters (`send_message`, WS
  frame rate, per-action buckets).
- `client_ip(request | websocket)` — the single IP helper. Trusts the first
  `X-Forwarded-For` hop **only when the direct peer is in `TRUSTED_PROXY_IPS`**
  (default the docker bridge range); otherwise the raw peer.
- `RateLimited` exception + a `main.py` handler → HTTP 429 with `Retry-After`.
  The WS path keeps emitting `{"type":"error","code":"rate_limited"}`.

### Locked limits (2026-09-06)

| Surface | Limit |
|---|---|
| Concurrent WS connections / user | hard cap **5**; 6th evicts the **oldest** (WhatsApp-style), never rejects the new one |
| `send_message` | **3 / second / user**, real sliding window; secondary `40 / 60 s` burst ceiling |
| `POST /auth/otp/request` | **5 / 30 min / phone** (down from 20 / 10 min) + **15 / hour / IP** |
| OTP / firebase verify | 5 / TTL / identity (exists) + 30 / hour / IP |
| `POST /auth/refresh` | 60 / hour / IP + 10 / hour / refresh-token jti |
| Account creation | 5 / day / IP |
| Media upload ticket | rate-limit only (**no volume quota**): 5 / 60 s / user + 20 / 60 s / IP |
| `GET /chats/{id}/messages` | `limit` clamped to `[1, 100]`; **30 pages / min / user** |
| WS inbound frame rate | 30 / 10 s / connection (drop frame, don't close; 60 consecutive over-limit → close `4429`) |
| WS per-action (per user) | `mark_*` 60 / 10 s · `subscribe_presence` 20 / 10 s · `typing`/`recording` 10 / 10 s · `edit`/`delete`/`restore` 20 / 60 s (siblings share a bucket) |
| WS handshake churn | 20 / 10 s / IP + 10 / 10 s / user on successful upgrades |
| Global REST backstop | 600 / 60 s / IP (Caddy or light middleware) |

Every number is an env var in `config.py`. Ship generous, tighten from metrics.

### WS connection cap mechanics (implemented step 5 — `services/ws_connection_registry.py`)

Redis `ZSET ws:conns:{user_id}`, member `"{server_id}:{connection_id}"`, score
= connect epoch-ms. On connect (post-auth, post-`accept()`) one Lua script:
`ZREMRANGEBYSCORE` older than `WS_CONN_MAX_AGE_SECONDS` (crash-leak sweep) →
`ZADD` self → while `ZCARD > WS_CONN_MAX_CONNECTIONS`, `ZPOPMIN` the overflow;
the caller publishes `force_disconnect` to each evicted member's
`instance_inbox:{server_id}`. `connection_manager._handle_force_disconnect`
(routed via the existing instance-inbox task) closes `4409` **silently** —
business answer 4 (2026-09-06) overrode the original "send a
`{"type":"disconnected"}` frame" design: no frame, no banner, just the close,
then idempotent local cleanup. Every `disconnect` `ZREM`s self (endpoint
`finally`). The atomic script makes N parallel opens converge to exactly the
newest `WS_CONN_MAX_CONNECTIONS`.

Handshake churn: `check_sliding_window` per IP (20/10 s) + per user (10/10 s),
after auth and before `accept()`; over → close `4429`. `PEXPIRE` in the Lua
script is skipped when max-age ≤ 0 (test guard — can't delete the fresh key).

### WS per-frame + per-action limits (implemented step 6 — `routers/websocket.py`)

- **Inbound frame rate**: receive loop, per `connection_id`, `ws_frame` 30/10 s
  sliding, checked before `_dispatch`. Over → `rate_limited` + drop the frame
  (not a close). A local strike counter (reset by any passing frame) closes
  `4429` after `WS_FRAME_FLOOD_STRIKES` (60) consecutive over-limit frames.
- **`send_message`**: `_handle_send_message` two-tier sliding — `send_message`
  3/1 s **and** `send_message_burst` 40/60 s. Replaces the old 20/10 s fixed
  window (`SEND_MESSAGE_RATE_LIMIT_*` now dead config).
- **Per-action**: `_ACTION_LIMITS` table in `_dispatch`, per user, sliding;
  siblings share a bucket (`mark_*`→`ws_receipts`, `edit`/`delete`/`restore`→
  `ws_edit`, `typing`/`recording`→`ws_typing`, plus `subscribe_presence`).
  Max/window read from module globals at call time so one knob is monkeypatchable.

### Transport hardening

- Caddy: HSTS (`max-age=31536000; includeSubDomains; preload`),
  `X-Content-Type-Options nosniff`, `X-Frame-Options DENY`,
  `Referrer-Policy strict-origin-when-cross-origin`, a minimal CSP for the
  single-file PoC, strip `Server`. `request_body { max_size 2MB }` on `@api`
  (uploads go straight to S3 via presigned PUT — the API never receives large
  bodies).
- App: `TrustedHostMiddleware` with `ALLOWED_HOSTS` (env). Fix CORS —
  default `CORS_ALLOW_ORIGINS` to the real origin; when it is `*` force
  `allow_credentials=False`. WebSocket `Origin` check **before** `accept()`
  against the same allowlist → close `4403`.

## Redis keyspace added

| Key | Type | Purpose |
|---|---|---|
| `rlsw:{action}:{id}` | zset | sliding-window log |
| `ws:conns:{user_id}` | zset | live WS connections for the 5-cap + evict-oldest |

(`ratelimit:{action}:{id}` string counter is unchanged.)

## Consequences

- Multi-worker-safe from day one; the demo's single worker is not special-cased.
- Two new Redis key families, both self-expiring / self-healing.
- One Lua script (sliding window) and one for the WS cap — both `EVALSHA`,
  one round trip each.
- HSTS `preload` commits the current host to HTTPS-forever (open business
  question 3 in COMMS_SECURITY_PLAN — may ship with a short `max-age` first).
- No schema change, no migration.
- Deferred to their own ADRs: message-content encryption at rest, semantic
  search / pgvector, full JWT hardening (rotation + reuse detection +
  revocation list — step 4 only rate-limits `/auth/refresh`), metadata
  protection.
