# Security & Rate Limiting

Read this before touching any rate limiter, the Caddyfile, CORS / TrustedHost /
`Origin` checks, or the WS connection cap.

Decision record: `docs/adr/0012-transport-hardening-and-rate-limiting.md`.

> **WS limits moved to the Rust gateway (ADR 0038):** every `ws_*` / `send_message`
> / connection-cap limit below is now enforced in `crates/ws_gateway`
> (`ws.rs` / `handlers.rs` / `message_ops.rs`), calling the **verbatim** Lua from
> `linka-common::ratelimit` — same Redis keys, windows and close codes. Mentions
> of `realtime/ws_router.py` / `ws_connection_registry.py` / `connection_manager.py`
> below are **historical** (those modules were deleted). `force_disconnect` is now
> handled by the gateway's `fanin` (fires the conn's cancel-notify → 4409). `main.py`
> also always appends `app`/`localhost`/`127.0.0.1` to `ALLOWED_HOSTS` so the
> gateway's `http://app:8000/internal/*` calls pass `TrustedHostMiddleware`.
Delivery plan: `COMMS_SECURITY_PLAN.md` (root) — Phase 1, 8 steps.
Status: **All 8 steps done** (docs, limiter engine, transport hardening,
auth/OTP limits, WS connection cap + handshake churn, WS per-frame + per-action
limits, REST feature limits, tests + rollout notes). Retune-knob table lives in
`deploy/README.md`. What IS live: security headers, `TrustedHostMiddleware`, the CORS
fix, the WS `Origin` check, the coarse per-IP REST backstop, all per-phone +
per-IP auth limits (step 4), the 5-connection cap + handshake-churn throttle
(step 5), the WS inbound-frame-rate + per-action limits (step 6), and the REST
history/upload-ticket/detail-read/list-read limits + history `limit` clamp
(step 7).

## Business answers (2026-09-06)

Per-IP REST backstop = **1000 / 180 s**. CORS locked to the single Caddy
origin (`*` dev-only → credentials off). HSTS **`max-age=86400` only**, no
`includeSubDomains` / `preload` while on shared sslip.io. Oldest WS connection
evicted **silently** (no UI/error). Account creation **5 / IP / day** (strict).
App container **locked to 1 CPU** (`cpus: 1.0`) — multi-worker-safe, bump later.

## Layering

- **Caddy** owns TLS, security headers, request-body ceilings, coarse per-IP
  ceilings (backstop only).
- **The app** owns every per-user / per-identity limit, in Redis (never
  in-process — multi-worker).

## Rate-limiter engine (`infra/ratelimit/service.py`)

| Function | Mechanism | Use |
|---|---|---|
| `check_and_increment(id, action, max, window_s)` → bool | fixed-window `INCR`+`EXPIRE`, one round trip, allows ≤2× boundary burst. Key `ratelimit:{action}:{id}` | coarse limits (OTP per-phone, per-IP ceilings) |
| `check_sliding_window(id, action, max, window_s)` → bool | Redis zset log via a `register_script` Lua (cached `EVALSHA`): `ZREMRANGEBYSCORE < now-window` → `ZCARD` → if under limit `ZADD`+`PEXPIRE`, atomic. Key `rlsw:{action}:{id}`. Member = `{now_ms}-{seq}` (per-process counter, avoids same-ms collisions) | anything where a boundary burst matters |
| `enforce_sliding_window(...)` → None | same, raises `RateLimited(action, retry_after)` instead of returning `False`; `retry_after` = whole seconds until the oldest hit ages out | call sites that want the exception |
| `client_ip(request \| websocket)` → str | first `X-Forwarded-For` hop, trusted **only** if direct peer ∈ `TRUSTED_PROXY_IPS` (default `172.16.0.0/12,127.0.0.1/32`); else raw peer; else `"unknown"` | every IP-keyed limit |
| `RateLimited(action, retry_after)` exception | `main.py` handler → HTTP 429 + `Retry-After` header + `{"detail","action"}`; WS path (not yet wired) → `{"type":"error","code":"rate_limited"}` | — |

## Every limit — key, window, enforcement point

| Limit | Bucket key | Window | Enforced | Status |
|---|---|---|---|---|
| `send_message` | `rlsw:send_message:{user_id}` + `rlsw:send_message_burst:{user_id}` | 3 / 1 s **and** 40 / 60 s (both must pass) | gateway `send_message` handler (two-tier) | **DONE (step 6)** |
| WS inbound frame rate | `rlsw:ws_frame:{connection_id}` | 30 / 10 s | receive loop, **before dispatch**; over → `rate_limited` + drop frame (no close); `WS_FRAME_FLOOD_STRIKES`=60 consecutive over-limit frames → close `4429` | **DONE (step 6)** |
| `mark_delivered`/`read`/`played` (combined) | `rlsw:ws_receipts:{user_id}` | 60 / 10 s | gateway dispatch, pre-handler | **DONE (step 6)** |
| `subscribe_presence` | `rlsw:ws_sub_presence:{user_id}` | 20 / 10 s | gateway dispatch | **DONE (step 6)** |
| `typing` / `recording` (combined) | `rlsw:ws_typing:{user_id}` | 10 / 10 s | gateway dispatch | **DONE (step 6)** |
| `edit`/`delete`/`restore_message` (combined) | `rlsw:ws_edit:{user_id}` | 20 / 60 s | gateway dispatch | **DONE (step 6)** |
| Concurrent WS conns / user | `ws:conns:{user_id}` (zset) | cap 5, evict oldest | gateway, on connect, the connection-cap Lua | **DONE (step 5)** |
| WS handshake churn | `rlsw:ws_upgrade_ip:{ip}` / `rlsw:ws_upgrade_user:{user_id}` | 20 / 10 s IP, 10 / 10 s user (sliding) | `/ws` after auth, before `accept()` + DB query; over → close `4429` | **DONE (step 5)** |
| `POST /auth/otp/request` | `ratelimit:otp_request:{phone}` (service) / `ratelimit:otp_request_ip:{ip}` (router) | 5 / 30 min phone, 15 / h IP | router (IP, pre-service) + `auth_service.request_otp` (phone) | **DONE (step 4)** |
| `POST /auth/otp/verify` & `/auth/firebase/verify` | `ratelimit:otp_verify:{phone}` / `firebase_verify:{phone}` (service, per OTP TTL) + `ratelimit:otp_verify_ip:{ip}` (router) | 5 / TTL phone, 30 / h IP | router (IP) + service (phone) | **DONE (step 4)** |
| `POST /auth/refresh` | `ratelimit:refresh_ip:{ip}` (router) + `ratelimit:refresh_jti:{jti}` (service) | 60 / h IP, 10 / h jti | router (IP) + `auth_service.refresh_access_token` (jti, before the SREM rotate check) | **DONE (step 4)** |
| Account creation | `ratelimit:acct_create:{ip}` | 5 / day IP | `auth_service._find_or_create_and_issue`, only when `get_user_by_phone` returns None (a returning login never trips it); `client_ip` threaded from the router | **DONE (step 4)** |
| `GET /chats/{id}/messages` | `rlsw:msg_history:{user_id}` | 30 / 60 s; `limit` clamped `[1, MSG_HISTORY_MAX_LIMIT]` (100) | `modules/messaging/router.py`, before the query | **DONE (step 7)** |
| Media upload ticket | `rlsw:upload_ticket:{user_id}` + `ratelimit:upload_ticket_ip:{ip}` | 5 / 60 s user (sliding), 20 / 60 s IP (fixed) | `modules/messaging/router.py` `create_media_upload_ticket`, before the participant check | **DONE (step 7)** |
| `GET .../{message_id}/receipts` (detail reads) | `rlsw:detail_read:{user_id}` | 60 / 60 s | `modules/messaging/router.py` | **DONE (step 7)** |
| `GET /chats`, `GET /users/me`, `GET /users/by-phone` | `rlsw:list_read:{user_id}` (shared bucket) | 120 / 60 s | `modules/chats/router.py` + `modules/users/router.py` | **DONE (step 7)** |
| Global REST backstop | `ratelimit:api_ip_backstop:{ip}` | 1000 / 180 s | `main.py` `_per_ip_backstop` HTTP middleware (skips `/healthz`, `/ws*`); stock Caddy has no `rate_limit` plugin | **DONE (step 3)** |

Legacy: `SEND_MESSAGE_RATE_LIMIT_MAX` / `_WINDOW_SECONDS` — the old 20/10 s
fixed-window send limit, **no longer read by any code** after step 6 (kept in
`config.py` only so a stale env file doesn't error). `OTP_REQUEST_RATE_LIMIT_MAX`
retuned to `5 / 1800 s` in step 4.

New exceptions from step 4 (all → HTTP 429): `RateLimited` (the router IP
gates raise it directly, `retry_after` = the window), and
`auth_service.AccountCreationRateLimitedError` (own handler in `main.py`).

## WS connection cap + handshake churn (step 5 — DONE)

**`realtime/ws_connection_registry.py`.** `ZSET ws:conns:{user_id}`, member
`"{server_id}:{connection_id}"`, score = connect epoch-ms. One `register_script`
Lua on connect (after auth + `accept()`): `ZREMRANGEBYSCORE` older than
`WS_CONN_MAX_AGE_SECONDS` (26h crash-leak sweep) → `ZADD` self → while
`ZCARD > WS_CONN_MAX_CONNECTIONS` (5): `ZPOPMIN` and collect → `PEXPIRE`
(guarded: skipped when max-age ≤ 0 so it can't delete the just-written key).
Returns the evicted members. All ops best-effort — a Redis error logs and
returns `[]`, the connection still proceeds.

`realtime/ws_router.py`: for each evicted member,
`realtime_service.publish_to_instance(server_id, {"event":"force_disconnect","connection_id":…,"reason":"connection_limit"})`.
`connection_manager._dispatch_inbox_event` routes a `force_disconnect` (no
`chat_id`) to `_handle_force_disconnect` → **close `4409` silently** (business
answer 4: no `disconnected` frame) + `disconnect()` (idempotent; the endpoint's
`finally` also cleans up + `ws_connection_registry.unregister` — `ZREM` self).
Atomic script ⇒ N parallel opens converge to the newest 5.

**Handshake churn**: `check_sliding_window` per IP (`ws_upgrade_ip`, 20/10 s)
and per user (`ws_upgrade_user`, 10/10 s), checked **after auth, before
`accept()`** and before the connect DB query. Over → close `4429`.
`get_all_chat_ids_for_user` gains a `limit` arg (`WS_MAX_CHAT_IDS_ON_CONNECT`
= 2000) as a defensive ceiling on that query.

## WS per-frame + per-action limits (step 6 — DONE)

- **Inbound frame rate** — `realtime/ws_router.py` receive loop, per
  `connection_id`, `check_sliding_window("ws_frame", 30, 10)` **before**
  `_dispatch`. Over the limit: send one `{"type":"error","code":"rate_limited"}`
  and **drop the frame** (do not dispatch, do not close — a laggy client that
  batches its sends is not an attacker). A local `frame_flood_strikes` counter
  increments on each consecutive over-limit frame and resets to 0 on any frame
  that passes; at `WS_FRAME_FLOOD_STRIKES` (60) → close `4429`.
- **`send_message`** — `_handle_send_message` runs its own two-tier sliding
  check: `send_message` (3 / 1 s, no boundary burst) **and** `send_message_burst`
  (40 / 60 s). Either failing → `rate_limited` (with `client_message_id`).
  Replaces the old single fixed-window check.
- **Per-action buckets** — `_ACTION_LIMITS` (`{msg_type: (action_key, MAX_global,
  WINDOW_global)}`) is consulted in `_dispatch` before the handler; the
  max/window are resolved from module globals at call time (so a test can
  monkeypatch one knob). Types sharing an `action_key` share a bucket
  (`mark_delivered`/`read`/`played` → `ws_receipts`; `edit`/`delete`/`restore`
  → `ws_edit`; `typing`/`recording` → `ws_typing`) so alternating siblings
  can't dodge the limit. Over → `{"type":"error","code":"rate_limited","for":<type>}`,
  handler skipped. `heartbeat` and `unsubscribe_presence` are intentionally
  unmetered (cheap, self-limiting).

## REST feature limits (step 7 — DONE)

All raise `RateLimited` → the existing HTTP 429 handler in `main.py`. Per-user
limits use `rate_limit_service.enforce_sliding_window`; the one per-IP gate
(upload ticket) uses `check_and_increment` + `client_ip(request)` and raises
`RateLimited` on `False`.

- **`modules/messaging/router.py`** `get_message_history`: `enforce_sliding_window(user_id,
  "msg_history", …)` **then** `limit = max(1, min(limit, MSG_HISTORY_MAX_LIMIT))`.
- **`modules/messaging/router.py`** `get_message_receipts`: `enforce_sliding_window(user_id,
  "detail_read", …)`.
- **`modules/messaging/router.py`** `create_media_upload_ticket` (now takes `request: Request`):
  per-user `enforce_sliding_window(user_id, "upload_ticket", …)` then per-IP
  `check_and_increment(ip, "upload_ticket_ip", …)`, both before the participant
  check / dedup lookup.
- **`modules/chats/router.py`** `list_my_chats` and **`modules/users/router.py`** `get_my_profile`
  / `get_profile_by_phone`: `enforce_sliding_window(user_id, "list_read", …)` —
  one shared `rlsw:list_read:{user_id}` bucket.

The step-3 global per-IP REST backstop (`_per_ip_backstop`, 1000 / 180 s) already
sits above all of these.

## Transport hardening (step 3 — DONE)

- **`deploy/Caddyfile`** `header` block on the site: `Strict-Transport-Security
  "max-age=86400"` (no `includeSubDomains`/`preload` — shared sslip.io parent),
  `X-Content-Type-Options nosniff`, `X-Frame-Options DENY`,
  `Referrer-Policy strict-origin-when-cross-origin`, a CSP tuned for the PoC
  (`'unsafe-inline'` + `https:` — it inlines its script/style and loads the Vue
  + Firebase SDKs), `-Server`. `request_body { max_size 2MB }` inside
  `handle @api` (uploads bypass the API via presigned PUT).
- **App** (`main.py`): `TrustedHostMiddleware` with `ALLOWED_HOSTS`
  (`"*"` → skipped; default `localhost,127.0.0.1,testserver,test`). CORS —
  `CORS_ALLOW_ORIGINS` list; when it is `["*"]` (dev) `allow_credentials` is
  forced `False` (the `*`+credentials combo is invalid). `_per_ip_backstop`
  HTTP middleware (see table).
- **`realtime/ws_router.py`**: `_origin_allowed(origin)` runs **before**
  `verify_access_token` and `accept()` — checks `websocket.headers["origin"]`
  against `CORS_ALLOW_ORIGINS`; `["*"]` allows any (incl. missing) Origin,
  otherwise a missing/foreign Origin → close **`4403`** (CSWSH protection).
- **`docker-compose.prod.yml`**: `app` service gets `cpus: 1.0`.
- Env additions in `deploy/env.production.example`: `CORS_ALLOW_ORIGINS`
  (now also the WS Origin allowlist), `ALLOWED_HOSTS`, `TRUSTED_PROXY_IPS`,
  `API_IP_BACKSTOP_MAX` / `_WINDOW_SECONDS`.

## WS close codes

`4403` bad `Origin` · `4409` connection-limit eviction (silent, no frame) ·
`4429` handshake churn (before `accept()`) **or** sustained inbound-frame flood
(`WS_FRAME_FLOOD_STRIKES` consecutive over-limit frames, in the receive loop).
(Existing: `4401` auth.) `4409` is emitted by
`connection_manager._handle_force_disconnect`; both `4429` cases by
`realtime/ws_router.py`.

## Config knobs

All in `config.py` next to the existing `*_RATE_LIMIT_*`.
Landed: `TRUSTED_PROXY_IPS` (list), `ALLOWED_HOSTS` (list), `CORS_ALLOW_ORIGINS`
(list — now parsed in `config.py`, was inline in `main.py`),
`API_IP_BACKSTOP_MAX` (1000), `API_IP_BACKSTOP_WINDOW_SECONDS` (180).
Step 4: `OTP_REQUEST_RATE_LIMIT_MAX` (5) / `_WINDOW_SECONDS` (1800),
`OTP_REQUEST_IP_RATE_LIMIT_MAX` (15) / `_WINDOW_SECONDS` (3600),
`OTP_VERIFY_IP_RATE_LIMIT_MAX` (30) / `_WINDOW_SECONDS` (3600),
`REFRESH_IP_RATE_LIMIT_MAX` (60) / `_WINDOW_SECONDS` (3600),
`REFRESH_JTI_RATE_LIMIT_MAX` (10) / `_WINDOW_SECONDS` (3600),
`ACCOUNT_CREATE_IP_RATE_LIMIT_MAX` (5) / `_WINDOW_SECONDS` (86400).
Step 5: `WS_CONN_MAX_CONNECTIONS` (5), `WS_CONN_MAX_AGE_SECONDS` (93600),
`WS_UPGRADE_IP_RATE_LIMIT_MAX` (20) / `_WINDOW_SECONDS` (10),
`WS_UPGRADE_USER_RATE_LIMIT_MAX` (10) / `_WINDOW_SECONDS` (10),
`WS_MAX_CHAT_IDS_ON_CONNECT` (2000).
Step 6: `WS_FRAME_RATE_MAX` (30) / `_WINDOW_SECONDS` (10), `WS_FRAME_FLOOD_STRIKES`
(60), `WS_SEND_MESSAGE_RATE_MAX` (3) / `_WINDOW_SECONDS` (1),
`WS_SEND_MESSAGE_BURST_MAX` (40) / `_WINDOW_SECONDS` (60),
`WS_RECEIPTS_RATE_MAX` (60) / `_WINDOW_SECONDS` (10),
`WS_SUBSCRIBE_PRESENCE_RATE_MAX` (20) / `_WINDOW_SECONDS` (10),
`WS_TYPING_RATE_MAX` (10) / `_WINDOW_SECONDS` (10),
`WS_EDIT_RATE_MAX` (20) / `_WINDOW_SECONDS` (60).
Step 7: `MSG_HISTORY_RATE_MAX` (30) / `_WINDOW_SECONDS` (60), `MSG_HISTORY_MAX_LIMIT`
(100), `UPLOAD_TICKET_RATE_MAX` (5) / `_WINDOW_SECONDS` (60),
`UPLOAD_TICKET_IP_RATE_LIMIT_MAX` (20) / `_WINDOW_SECONDS` (60),
`DETAIL_READ_RATE_MAX` (60) / `_WINDOW_SECONDS` (60), `LIST_READ_RATE_MAX` (120)
/ `_WINDOW_SECONDS` (60). Routers import these names directly, so a test
monkeypatches `routers.<module>.<NAME>`.

## Tests + rollout (step 8 — DONE)

Coverage: `crates/common` ratelimit tests + `test_internal_router.py`; historically `test_ws_connection_registry.py` (6th evicts oldest, N parallel opens
converge to the cap, stale sweep), `test_rest_api.py` (OTP spray per IP,
returning login not blocked by the account-creation cap, history `limit`
clamped, history 429, upload-ticket per-IP 429), `test_websocket.py`
(frame-flood drops frames then closes `4429`, handshake churn `4429`,
connection cap evicts `4409` silently, bad Origin `4403`, `send_message`
two-tier + per-action buckets), `test_rate_limit_sliding_window.py` (boundary,
slide, concurrency, `EVALSHA` script reuse). Rollout: every limit is an env var,
defaults are generous, retune-knob table + WS close codes in `deploy/README.md`.

## Scheduled messages (ADR 0031)
- `scheduled_write` sliding bucket — `SCHEDULED_WRITE_RATE_MAX` (20) / `SCHEDULED_WRITE_RATE_WINDOW_SECONDS` (60), per user, enforced in `modules/messaging/router.py` on `POST`/`PATCH`/`DELETE` of scheduled messages → HTTP 429 via the existing handler.
- Non-rate abuse ceilings live in `scheduled_service`: `SCHEDULED_MAX_PENDING_PER_USER` (100 pending rows/user → `ScheduledLimitExceededError` 409), `SCHEDULED_MIN_LEAD_SECONDS` (10) / `SCHEDULED_MAX_LEAD_DAYS` (365) time bounds → `ScheduledTimeInvalidError` 400.
- Scheduled sends bypass the WS per-user `send_message` limiter (server-originated at fire time) but still traverse the send stream + `send_message_burst` window downstream.

## Deferred (own ADRs, not Phase 1)

Message-content encryption at rest · semantic search / pgvector · full JWT
hardening (refresh rotation + reuse detection + revocation list) · metadata
protection.
