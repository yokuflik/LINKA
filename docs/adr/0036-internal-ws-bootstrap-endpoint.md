# ADR 0036 — Internal `ws-bootstrap` endpoint for the Rust gateway

Status: Accepted
Date: 2026-09-10

## Context

The Rust `ws_gateway` (ADR 0033) must, on every connect, learn the set of chat
ids a user belongs to — the same list the Python `/ws` endpoint gets from
`get_all_chat_ids_for_user(session, user_id, limit=WS_MAX_CHAT_IDS_ON_CONNECT)`
— so it can populate its local `chat_subs` table and register the process in
`chat_instances:{chat_id}`.

ADR 0033 deferred *how* to RUST_WS_GATEWAY_PLAN.md step 5a. Three options were
weighed: a direct Postgres query from Rust (breaks the "two Redis connections,
no DB" resource budget, puts DB credentials in the gateway), a Redis-cached
`user_chats:{uid}` set (new denormalised state to keep consistent on every
join/leave/create path), or a thin internal HTTP call back to the Python app.

## Decision

Add `GET /internal/ws-bootstrap?token=<access-jwt>` to the FastAPI app
(`realtime/internal_router.py`). It verifies the token with the existing
`auth_service.verify_access_token` and returns:

```json
{ "user_id": "<snowflake>", "chat_ids": ["<snowflake>", ...] }
```

The gateway calls it once per connect over plain HTTP on the internal Docker
network (`APP_INTERNAL_URL`, default `http://app:8000`), with a 5 s timeout; a
failure logs and yields an empty chat list (the connection still opens, live
chat events just don't reach it until the client reconnects — same failure mode
as a dropped routing registration).

### Why this and not the alternatives

- Reuses the one query that already defines "a user's chats" — zero risk of the
  gateway's view drifting from the Python view.
- No schema change, no new denormalised Redis state, no DB driver in Rust.
- Volume is one request per WS connect (already rate-limited upstream by the
  `ws_upgrade_*` sliding windows), not per message.

### Security

The `/internal` prefix is **never exposed publicly**: Caddy only proxies `/ws`,
`/api`-shaped paths and the existing public routes to their backends and returns
404 for `/internal*` (added in RUST_WS_GATEWAY_PLAN.md step 7 alongside the
`/ws` → gateway route). `ws-bootstrap` additionally verifies the caller's JWT
(it echoes the user's own chat list). The Step 6 helpers added under the same
prefix — `presence-authorized`, `typing-allowed` — take plain id query params
and rely solely on the edge block + internal network (they expose only a single
authz boolean, never bulk data); if `/internal` ever needs to be reachable from
a less-trusted network, gate the whole prefix with a shared secret header. No
new secret is introduced now.

## Consequences

- The gateway now has one outbound HTTP dependency (`reqwest`,
  `default-features = false`, no TLS — internal plaintext only). This is a
  deliberate, documented deviation from ADR 0033's "two Redis connections
  total": it is connect-path only, never on the message hot path.
- `realtime/internal_router.py` is a new router mounted in `main.py`, the
  `/internal` namespace. Gateway Step 6 added `/internal/presence-authorized`
  and `/internal/typing-allowed` here; the shared `privacy.online` rule moved to
  `realtime/presence_authz.py` (imported by both this router and `ws_router`).
- Deployment must keep `/internal*` unroutable from the edge; documented in
  `.claude_docs/deployment.md` and enforced in the Caddyfile at step 7.

## Alternatives considered

- **Direct Postgres from Rust** — breaks the resource budget, spreads DB
  credentials, duplicates the participant query.
- **Redis `user_chats:{uid}` set** — every join/leave/create/delete path in
  Python must keep it in lockstep; a missed write silently breaks fan-in.
