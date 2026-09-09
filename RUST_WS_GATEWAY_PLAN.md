# Rust WebSocket Gateway — Delivery Plan

Companion to **ADR 0033**. Replaces the FastAPI `/ws` endpoint with a standalone
Rust service (`ws_gateway`) that speaks the existing Redis contract unchanged.
Python REST + send/fan-out/receipt/scheduled workers are untouched.

Guiding rule: **the Rust process is indistinguishable from a Python process to
the routing layer** — it registers in `chat_instances:{chat_id}` and consumes
`instance_inbox:{server_id}` the same way. Both WS implementations can run at
once (path-based canary) during migration.

---

## Target workspace tree

```
LINKA/
├── Cargo.toml                     # [workspace] + [profile.release]
├── Cargo.lock                     # single lockfile
├── rust-toolchain.toml            # stable + x86_64-unknown-linux-musl
├── target/                        # single shared, git-ignored
├── proto/snowflake.proto          # unchanged
├── crates/
│   ├── common/
│   │   ├── Cargo.toml
│   │   └── src/
│   │       ├── lib.rs
│   │       ├── config.rs          # env loader (JWT_SECRET, REDIS_URL, CORS_ALLOW_ORIGINS, shard counts, limits)
│   │       ├── auth.rs            # Claims struct + HS256 verify (jsonwebtoken)
│   │       ├── redis_keys.rs      # every key/channel/stream name builder + shard_for_chat
│   │       ├── events.rs          # serde enums: ClientFrame (inbound) / ServerEvent (outbound to client)
│   │       └── ratelimit_lua.rs   # sliding-window + ws:conns cap script wrappers
│   ├── id_service/                # git mv ./id_service → here; build.rs proto path stays ../../proto
│   │   ├── Cargo.toml
│   │   ├── build.rs
│   │   └── src/main.rs
│   └── ws_gateway/
│       ├── Cargo.toml
│       └── src/
│           ├── main.rs            # tokio runtime, axum router, lifespan, SIGTERM drain
│           ├── state.rs           # AppState, ConnHandle, DashMaps, ConnId/UserId/ChatId newtypes
│           ├── ws/
│           │   ├── mod.rs
│           │   ├── handshake.rs   # Origin → JWT → churn limits → conn-cap → accept
│           │   ├── reader.rs      # inbound loop: per-frame bucket → dispatch
│           │   ├── writer.rs      # mpsc<ServerFrame> → SplitSink task
│           │   └── dispatch.rs    # match ClientFrame → handler; per-action Redis limiter
│           ├── handlers/
│           │   ├── send_message.rs   # XADD message_send_stream (sharded)
│           │   ├── receipts.rs       # XADD receipt_log_stream
│           │   ├── typing.rs         # publish_event replication
│           │   ├── presence.rs       # presence_active / subscribe_presence / heartbeat
│           │   └── mod.rs
│           ├── redis/
│           │   ├── mod.rs
│           │   ├── inbox.rs       # SUBSCRIBE instance_inbox:{server_id} → dispatch to chat_subs
│           │   ├── pubsub.rs      # one pub/sub conn; dynamic SUB/UNSUB registry actor
│           │   ├── routing.rs     # register/heartbeat/unregister chat_instances + instance_chats
│           │   └── publish.rs     # publish_event(chat_id, event): SMEMBERS → PUBLISH per instance
│           ├── ratelimit.rs       # local per-conn token buckets + Redis Lua calls
│           └── reaper.rs          # idle-connection sweeper (last_seen vs heartbeat window)
├── deploy/
│   ├── ws_gateway.Dockerfile      # musl build stage → distroless/static
│   └── README.md                  # + gateway build/deploy/rollback steps
└── docker-compose.prod.yml        # + ws_gateway service; Caddy /ws → gateway
```

---

## Steps

### Step 1 — ADR + docs (DONE)
- ADR 0033, this plan, CLAUDE.md ADR index row.
- `.claude_docs/realtime_and_redis.md` + `.claude_docs/deployment.md` pointers.

### Step 2 — Workspace skeleton (DONE)
- Root `Cargo.toml` `[workspace]` (members: `crates/common`, `crates/ws_gateway`,
  `id_service`) + `[profile.release]` (opt-level=z, lto=fat, codegen-units=1,
  panic=abort, strip) + `[workspace.dependencies]`.
- **Deviation:** `id_service` left in place (workspace member at `./id_service`),
  NOT `git mv`'d to `crates/`. Sharing one `target/` needs only workspace
  membership, not colocation; the move touched the working Dockerfile +
  compose for zero functional gain. Its standalone `Dockerfile` build still
  works (it never copies the workspace root). Revisit if it causes friction.
- `rust-toolchain.toml` (stable + `x86_64-unknown-linux-musl`).
- `.gitignore`: root `/target/`. Old `id_service/target/` (788 MB) deleted.
- `crates/common` (config/auth/redis_keys/events) + `crates/ws_gateway`
  (`main.rs` boots tokio, loads config, connects Redis, serves `/healthz`;
  `state.rs` = `AppState` + DashMaps).
- **Verified:** `cargo check --workspace` green (only dead-code warnings on the
  skeleton), `cargo test -p linka-common` 3/3 pass, `cargo check -p id_service`
  unchanged, single root `target/`.

### Step 3 — `common`: the Python↔Rust seam (IN PROGRESS)

Captured so far (from code):
- `message_send_stream` XADD fields (`send_queue.enqueue_outgoing_message`):
  `chat_id, sender_id, client_message_id, content, type, reply_to_message_id,
  media_key, media_name, media_duration_seconds, media_blur_hash, enc_header`
  — all stringified, `None`→`""`, `enc_header` = `json.dumps(enc)` or `""`.
  Key `message_send_stream`, shard `chat_id % SEND_STREAM_SHARDS` (default 4),
  shard 0 = bare key, maxlen ~1_000_000 approximate. → `events::SendStreamEntry`.
- `receipt_log_stream` XADD fields (`enqueue_receipt_event`): `chat_id, user_id,
  kind, up_to_message_id, occurred_at` (ISO-8601 UTC). kinds 2/3/4 =
  delivered/read/played. → `events::ReceiptStreamEntry`.
- JWT: HS256, `JWT_SECRET`, claims `{sub: str user-id, exp}`. → `auth`.

Done since: `ratelimit.rs` (both Lua scripts verbatim + `RateLimiter`),
`config::Limits` (every WS bucket, defaults from `security_settings.py`),
`redis_keys` (routing / presence / ws:conns / stream-shard helpers),
`events::{SendStreamEntry, ReceiptStreamEntry, ClientFrame}`.

Still to do:
- `enc_header` re-parse parity check with `SendWorker._rebuild_enc_header`.
- Full `ServerEvent` enum coverage + the golden-JSON cross-language test
  (`pytest` fixture dump ↔ Rust round-trip).

Original checklist:
- `config.rs` — env reads, defaults matching `config/` (`SEND_STREAM_SHARDS` 4,
  `FANOUT_STREAM_SHARDS` 4, `CHAT_INSTANCE_TTL_SECONDS` 90,
  `ROUTING_HEARTBEAT_INTERVAL_SECONDS` 30, `WS_CONN_MAX_CONNECTIONS` 5, per-frame
  30/10, per-action table, upgrade churn 20/10 & 10/10).
- `auth.rs` — `Claims { sub, exp }`, HS256 verify against `JWT_SECRET`.
- `redis_keys.rs` — builders for: `message_send_stream[:{n}]`,
  `message_fanout_stream[:{n}]`, `receipt_log_stream`, `chat_instances:{id}`,
  `instance_chats:{sid}`, `instance_inbox:{sid}`, `user_events:{uid}`,
  `presence_events:{uid}`, `presence:{uid}`, `presence_last_seen:{uid}`,
  `ws:conns:{uid}`; `shard_for_chat(chat_id, n)` (shard 0 → bare key).
- `events.rs` — `ClientFrame` enum (`send_message`, `mark_delivered`/`mark_read`/
  `mark_played`, `typing`/`recording`, `subscribe_presence`/`unsubscribe_presence`/
  `presence_active`, `heartbeat`, `edit_message`/`delete_message`/`restore_message`/
  `purge_message`) and `ServerEvent` (`ack`, `new_message`, receipt events,
  `typing`, `presence_status`/`presence_update`/`presence_revoked`, `rate_limited`,
  `error`, `force_disconnect`, `message_failed`/`message_already_sent`,
  `scheduled_message_sent`/`scheduled_message_failed`, …).
- **From code, pin exact field names/types** of the `message_send_stream` XADD
  (read `realtime/fanout/send_queue.py` + `SendWorker`) and
  `receipt_log_stream` (`enqueue_receipt_event`). Record them in `redis_keys.rs`
  doc comments.
- `ratelimit_lua.rs` — load the exact sliding-window + `ws:conns` scripts from
  `infra/ratelimit/` (copy the `.lua` / inline strings verbatim), `redis::Script`
  wrappers.
- **Verify:** golden-JSON test harness — a `pytest` marker dumps fixtures from
  the real Python event builders into `tests/fixtures/ws_events/`; a
  `crates/common` test deserialises + re-serialises each and asserts equality.

### Step 4 — Connection lifecycle (no Redis fan-in yet) — CODE DONE, live test pending

Landed: `ws_gateway/src/ws.rs` (handshake → reader loop → dispatch → RAII
cleanup, all in one module — the plan's 5-file `ws/` split deferred until it
grows past ~300 lines), `send_path.rs` (sharded `XADD message_send_stream`),
`state.rs` (`AppState` + `add_conn`/`remove_conn`/`publish_to_instance`).
- Handshake order: Origin (4403, pre-upgrade) → JWT (4401) → `ws_upgrade_ip` +
  `ws_upgrade_user` sliding window (4429) → split socket + spawn writer task →
  `ws:conns` cap Lua → publish `force_disconnect` per evicted member → local
  maps + `presence` SADD/EXPIRE.
- **Deviation:** rejections upgrade-then-close with the 4xxx code (parity with
  the Python client experience) rather than returning an HTTP status.
- Reader loop: `ws_frame` 30/10 sliding window + `frame_flood_strikes` close;
  `bad_frame` error on parse failure.
- Dispatch: `heartbeat` (presence EXPIRE refresh + `heartbeat_ack`) and
  `send_message` (two-tier `send_message` 3/1 + `send_message_burst` 40/60 →
  `XADD` → `{"type":"ack","for":"send_message","status":"queued"}`) are live.
  `mark_*` / `typing` / `presence_*` consume their bucket then no-op (Steps 5–6).
- Cleanup: `remove_conn` (drops from `conns`/`user_conns`/`chat_subs`, prunes
  empty index entries) + `presence` SREM + `ws:conns` ZREM + `writer.abort()`.
- Presence edge publishes, the `reaper` task, and `last_seen` tracking are
  Step 6 — connect currently does a plain SADD without the 0→1 `presence_update`.
- **Verified:** `cargo check/clippy/test --workspace` clean (3 common unit
  tests). Live WS-client + Redis integration test still owed — run once
  `docker compose up` stack is available.

Original checklist:
- `state.rs` — `AppState`, `ConnHandle`, three `DashMap`s, newtypes.
- `ws/handshake.rs` — axum `WebSocketUpgrade`; order: Origin check (4403) → JWT
  (4401) → `ws_upgrade_ip` + `ws_upgrade_user` Lua (4429) → `accept()` →
  `ws:conns` cap Lua (collect evicted) → register in maps → for each evicted
  member `publish_to_instance(sid, force_disconnect)`.
- `ws/writer.rs` — owns `SplitSink`, drains `mpsc`, handles close.
- `ws/reader.rs` — `SplitStream` loop: local `ws_frame` token bucket (30/10,
  strike-close 4429) → parse `ClientFrame` → `dispatch`.
- `ws/dispatch.rs` — per-action Redis sliding-window check
  (`ws_receipts`/`ws_edit`/`ws_typing`/`ws_sub_presence`, `send_message` 3/1 +
  `send_message_burst` 40/60) then handler; over → `rate_limited`, drop frame.
- Drop guard: remove from `conns` / `user_conns` / `chat_subs`,
  `remove_if` empty, `SREM chat_instances` on the `1→0` edge.
- `reaper.rs` — sweep `last_seen`.
- Handlers that need no fan-in yet: `heartbeat` (refresh `presence:{uid}` TTL),
  `send_message` (XADD only — Python worker does the rest and the echo comes
  back once fan-in lands).
- **Verify:** integration test with a real WS client (tokio-tungstenite) against
  a test Redis — connect/auth/cap-evict/disconnect cleanup; `send_message`
  produces a well-formed `message_send_stream` entry the Python `SendWorker`
  test can consume.

### Step 5 — Routing layer + Redis fan-in — CODE DONE, live mixed-fleet test pending

Landed:
- **5a resolved → ADR 0036**: `GET /internal/ws-bootstrap?token=` on the Python
  app (`realtime/internal_router.py`, mounted in `main.py`) returns
  `{user_id, chat_ids}` via the same `get_all_chat_ids_for_user`. Gateway calls
  it with `reqwest` (`default-features=false`, no TLS — internal plaintext),
  5 s timeout, failure → connect with empty chat set. `APP_INTERNAL_URL` env
  (default `http://app:8000`). **`/internal*` must 404 at the edge — Caddy rule
  is Step 7.**
- `routing.rs` — `add_chat` / `remove_chat` (0↔1 edges) / `heartbeat` /
  `unregister`, field-for-field `realtime/fanout/routing.py`. Heartbeat task in
  `main.rs` every `ROUTING_HEARTBEAT_INTERVAL_SECONDS`; `unregister` after
  `axum::serve` returns (SIGTERM/ctrl-c both wired).
- `fanin.rs` — the **single** pub/sub task (own `redis::Client` connection,
  `pubsub.split()`), resilient-resubscribe loop. Always `SUBSCRIBE
  instance_inbox:{server_id}`; a `mpsc<SubCmd>` from connect/teardown does the
  dynamic `user_events:{uid}` SUB/UNSUB on the user's 0↔1 local-conn edge.
  Message routing: `force_disconnect` (no chat_id) → `AppState::force_disconnect`
  fires the conn's `Notify`, reader queues a 4409 `ServerFrame::Close`;
  `user_events:` channel → `added_to_chat`/`removed_from_chat` adjust local
  `chat_subs` + routing, then forward verbatim to the user's conns;
  chat-scoped (`chat_id` present) → `senders_for_chat` → `try_send`.
- `state.rs` — `ServerFrame` is now `Text(String)|Close(u16)`; `add_conn`
  returns "user's first conn here"; `remove_conn` returns
  `(chats_now_empty, user_gone)`; `senders_for_chat`/`senders_for_user` clone
  senders out from under the guard; `publish_event` (Python
  `realtime_service.publish_event` twin) added for Step 6's typing handler
  (`#[allow(dead_code)]` until then).
- **Deviations from the plan tree**: one `routing.rs` + one `fanin.rs` instead
  of a 4-file `redis/` dir (same rationale as Step 4's single `ws.rs`);
  `presence_events:{uid}` subscriptions deferred to Step 6 (no presence fan-in
  yet); `reqwest` is a documented 3rd I/O dependency beyond ADR 0033's "two
  Redis connections" — connect-path only.
- **Verified**: `cargo build/clippy/test --workspace` clean (3 common unit
  tests; clippy clean for `ws_gateway`). Live test (Python `SendWorker` fan-out
  → Rust client; Rust ↔ Python mixed `typing`) still owed — run once the full
  `docker compose up` stack + a WS client are available.

Original checklist:
- `redis/routing.rs` — on first local subscriber to a chat:
  `SADD chat_instances:{chat_id} {server_id}` + `EXPIRE 90` +
  `SADD instance_chats:{server_id} {chat_id}`; `_routing_heartbeat` task every
  30 s re-`EXPIRE`s all; SIGTERM → `unregister_instance` (`SPOP`/`DEL`
  `instance_chats`, `SREM` self from each `chat_instances`).
- On connect, resolve the user's chat ids (call a tiny Python REST endpoint
  `GET /internal/ws-bootstrap` returning chat ids + presence-authorised targets,
  OR query Redis if a set exists — **decide in step 5a**; Python currently uses
  `get_all_chat_ids_for_user`, so a minimal internal endpoint is cleanest) and
  populate `chat_subs`.
- `redis/pubsub.rs` — single pub/sub connection; registry actor owns
  `SUBSCRIBE`/`UNSUBSCRIBE` for `instance_inbox:{server_id}` (always),
  `user_events:{uid}` (per connected user, 0↔1), `presence_events:{uid}` (per
  active presence subscription).
- `redis/inbox.rs` — parse each pub/sub message → `ServerEvent`; for chat-scoped
  events look up `chat_subs[event.chat_id]`, clone senders, `try_send`;
  `force_disconnect` (no `chat_id`) → close 4409 silently; `user_events` →
  route to that user's connections; `presence_events` → to subscribers.
- `redis/publish.rs` — `publish_event(chat_id, mut event)`: inject `chat_id`
  string, `SMEMBERS chat_instances:{chat_id}`, `PUBLISH instance_inbox:{sid}`
  the JSON to each.
- **Verify:** Python `SendWorker` (unmodified) fan-out of a `new_message`
  reaches a Rust-connected client; a Rust `typing` reaches a Python-connected
  client and vice-versa (mixed-fleet test).

### Step 6 — Presence + typing — CODE DONE, live test pending

Landed:
- `presence.rs` — `mark_online`/`mark_offline`/`heartbeat`/`set_active`/`get_status`,
  twin of `realtime/presence_service.py`. `presence:{uid}` = TTL'd SET of **bare
  connection ids** (matches Python, NOT the `{sid}:{conn}` form — that was a
  Step 4 bug, now fixed); `presence_last_seen:{uid}` = RFC3339 (`time` crate);
  `presence_update` published on `presence_events:{uid}` only on the 0↔1 device
  edge. New config `PRESENCE_TTL_SECONDS` (60). The dead `presence_srv:` key is
  not written (no reader).
- `handlers.rs` — `typing` (+ `recording_audio`) and `subscribe_presence`.
  Dispatch `match` stays in `ws.rs`; handlers split out (ws.rs was >300 lines).
- `state.rs` — `presence_subs` (target→local watchers) + `presence_watches`
  (conn→targets) maps; `add/remove_presence_watch` (0↔1 edges),
  `senders_for_presence`; `remove_conn` now returns a 3rd element
  (presence targets emptied → unsubscribe channel).
- `fanin.rs` — dynamically SUB/UNSUB `presence_events:{uid}` via the same
  `mpsc<SubCmd>`; a `presence_events:` message → `senders_for_presence`.
- **Two new internal endpoints** (`realtime/internal_router.py`, ADR 0036):
  `GET /internal/presence-authorized?watcher_id=&target_user_id=` (wraps the new
  shared `realtime/presence_authz.py`, also used by `ws_router`) and
  `GET /internal/typing-allowed?chat_id=&sender_id=` (full server-side gate:
  participant + 1:1 privacy). Gateway calls `typing-allowed` **per typing
  event** (fail-closed on error) — a short-TTL local participant cache is the
  obvious next optimisation (risk 3).
- `presence_active` / `subscribe_presence` consume `ws_sub_presence`;
  `unsubscribe_presence` unmetered; `typing`/`recording` consume `ws_typing` —
  all matching Python.
- **Receipts → async redesign — DONE (ADR 0037, 2026-09-10).** The gateway's
  `mark_delivered`/`mark_read`/`mark_played` now `XADD receipt_log_stream`
  (`crates/ws_gateway/src/receipts.rs`, `ReceiptStreamEntry` shape) + rate-limit
  bucket + ack, nothing else. `MarkFrame` field renamed `up_to_message_id` →
  `message_id` (the client's actual field; old name kept as a serde alias) —
  Step 3's events.rs had it wrong. The **existing** Python `receipt_log` worker
  (`drain_once` → new `modules/receipts/apply.py`) now also advances the coarse
  watermark, does the ADR 0003 privacy gate, and publishes the live receipt
  event — for both fleets. Python `_handle_mark_*` + `mark_as_{delivered,read,
  played}` collapsed to the same enqueue. 455 Python tests pass.
- **Verified**: `cargo build/clippy/test --workspace` clean; Python
  `pytest -k "presence or typing or ws_router or websocket"` = 51 passed. Live
  presence/typing mixed-fleet test still owed.

Original checklist:

### Step 6 — Presence + typing (original)
- `handlers/presence.rs`:
  - `presence_active {active}` → `SADD`/`SREM presence:{uid} {sid}:{conn}`,
    `_touch_last_seen`, publish `presence_update` on the target's own
    `presence_events:{uid}` only on the 0↔1 edge.
  - `subscribe_presence {user_id}` → authorise via the same rule
    (`privacy.online`: needs a Python `GET /internal/presence-authorized`
    helper, or replicate the `contacts` pair-chat lookup) → register the
    pub/sub subscription → push `presence_status` pull, then updates.
    Re-checked on the client's heartbeat re-subscribe; loss → `presence_revoked`.
  - `unsubscribe_presence` → drop subscription.
- `handlers/typing.rs` — load participants (internal helper or cached), sender-
  is-participant check, 1:1 privacy gate (sender's `privacy.online`), then
  `publish_event(chat_id, {event:"typing", chat_id, user_id, kind})`.
- **Verify:** presence online/offline edges, multi-device (offline only when all
  gone), privacy `nobody`/`contacts`/`everyone`, typing 1:1 gate + group
  exemption — port the Python tests.

### Step 7 — Build + deploy — FILES DONE, off-host image build + server rollout pending

Landed:
- `deploy/ws_gateway.Dockerfile` — `rust:1-slim` builder, `musl-tools` +
  `rustup target add x86_64-unknown-linux-musl`, BuildKit cache mounts
  (`/usr/local/cargo/registry` + `/build/target`), copies the whole workspace
  (`Cargo.toml`/`Cargo.lock`/`rust-toolchain.toml` + `crates/` + `id_service/` +
  `proto/`), `cargo build --release -p ws_gateway --target …-musl`. Runtime =
  `gcr.io/distroless/static-debian12:nonroot`, single binary, `EXPOSE 8081`.
- **Healthcheck without a shell:** `ws_gateway healthcheck` subcommand
  (`main.rs::health_probe`) — a dependency-free blocking HTTP/1.0 GET to the
  local `/healthz`, exit 0/1. `HEALTHCHECK CMD ["/ws_gateway","healthcheck"]` in
  both the Dockerfile and the compose service.
- `.dockerignore` — added `target/` + `**/target/` (was missing; the Python
  `COPY . .` would otherwise pull the multi-GB Rust build dir).
- `docker-compose.prod.yml` — `ws_gateway` service: `image:
  ${WS_GATEWAY_IMAGE:-linka-ws-gateway:latest}` (server sets the registry ref),
  `build:` kept for local builds only, `mem_limit: 64m`, `cpus: 0.5`, env
  `REDIS_URL` / `JWT_SECRET=${JWT_SECRET_KEY}` / `CORS_ALLOW_ORIGINS` /
  `APP_INTERNAL_URL=http://app:8000` / `WS_GATEWAY_BIND`, `SERVER_ID` unset.
  `depends_on` redis(healthy)+app(started); `caddy` now also `depends_on`
  ws_gateway.
- `deploy/Caddyfile` — `handle /internal* { respond 404 }`; `handle /ws` →
  `ws_gateway:8081`; `handle /ws-legacy` → `rewrite * /ws` → `app:8000`
  (rollback); `/ws` removed from the `@api` matcher.
- `deploy/README.md` — gateway build/push/pull/rollback runbook + the 2 GB
  swapfile step + the updated memory budget.

Still to do (off the dev machine / on the server):
- `docker buildx build --platform linux/amd64 -f deploy/ws_gateway.Dockerfile
  -t <registry>/linka-ws-gateway:<tag> --push .` then set `WS_GATEWAY_IMAGE` in
  the server `.env` and `docker compose pull ws_gateway && up -d`.
- Add the 2 GB swapfile to the t3.micro **before** the first `up -d` with the
  new service (RAM headroom is tight — see the budget in `deploy/README.md`).
- Staging canary: point a fraction of PoC clients at `/ws` (Rust), rest at
  `/ws-legacy`; watch gateway RSS, reconnect rate, message loss.

Original checklist:

- `deploy/ws_gateway.Dockerfile` — `FROM rust:1-slim AS builder`,
  `rustup target add x86_64-unknown-linux-musl`, BuildKit cache mounts for
  `/usr/local/cargo/registry` + `/build/target`,
  `cargo build --release -p ws_gateway --target x86_64-unknown-linux-musl`;
  runtime `FROM gcr.io/distroless/static-debian12`, copy the one binary,
  `EXPOSE 8081`, `HEALTHCHECK` on `/healthz`.
- Built on the dev Mac via `docker buildx --platform linux/amd64` (or CI) →
  registry → server pulls. **Never `cargo build` on the host.**
- Add a 2 GB swapfile to the t3.micro (`deploy/README.md`).
- `docker-compose.prod.yml` — `ws_gateway` service (`mem_limit: 64m`,
  `cpus: 0.5`), env `REDIS_URL`, `JWT_SECRET`, `CORS_ALLOW_ORIGINS`,
  `SERVER_ID` unset (self-gen UUID).
- Caddyfile — `/ws*` → `ws_gateway:8081`; keep `/ws-legacy*` → Python app for
  rollback.
- **Verify:** staging canary — a fraction of PoC clients pointed at `/ws`
  (Rust), the rest at `/ws-legacy`; watch RSS, reconnect rate, message loss.

### Step 8 — Cutover + cleanup — PARTIALLY DONE (blockers cleared; live canary owed)

Landed:
- **Receipt async redesign — ADR 0037** (the last hard blocker): `mark_*` is
  fire-and-forget on both fleets, the `receipt_log` worker does watermark +
  privacy + live event. 455 tests pass.
- **Import guard**: `config.LEGACY_WS_ENABLED` (`config/app_settings.py`,
  default **true**); `main.py` mounts the FastAPI `/ws` only when set. Caddy
  already serves it at `/ws-legacy`. Flip to `false` to unmount at cutover.

Still to do (needs the live canary from Step 7 first):
- Off-host image build + server rollout + canary (Step 7 tail).
- Once the gateway is proven under canary: flip PoC clients fully to `/ws`, set
  `LEGACY_WS_ENABLED=false`, watch a release.
- Follow-up ADR/PR deletes `realtime/ws_router.py` +
  `realtime/connection_manager.py` + `ws_connection_registry.py` +
  `presence_service.py` (gateway owns all of it) once stable.
- Final docs pass: `.claude_docs/security_and_rate_limiting.md` (limiter now
  also called from Rust), `realtime_and_redis.md` + `deployment.md` (drop the
  "in progress" framing).

---

## Risks / open decisions

| # | Item | Resolution point |
|---|---|---|
| 1 | Exact `message_send_stream` / `receipt_log_stream` XADD field names + types | Step 3, read from code |
| 2 | How the gateway learns a user's chat ids + presence authorisation on connect | **RESOLVED (ADR 0036)**: `GET /internal/ws-bootstrap` on the Python app, JWT-verified, edge-blocked. Presence-auth helper (`/internal/presence-authorized`) joins it in Step 6 |
| 3 | Participant list for typing / send checks | Step 6 shipped **per-event internal call** (`/internal/typing-allowed`); short-TTL local cache is a follow-up optimisation, not yet needed |
| 7 | Receipts (`mark_*`) on the gateway | **DONE — ADR 0037.** Gateway `XADD receipt_log_stream` only; the existing `receipt_log` worker (`+ modules/receipts/apply.py`) now does watermark + ADR 0003 mask + live event for both fleets. `mark_*` wired in the gateway. No longer blocks Step 8 |
| 4 | Redis reconnection: `redis-rs` bare reconnect vs switching to `fred` | Step 5, escalate only if flaky |
| 5 | Does `ws_gateway` also need to consume `message_fanout_stream` directly, or is `instance_inbox` sufficient? (It is sufficient — fan-out is Python's job.) | Confirmed: inbox only |
| 6 | Graceful drain on deploy: SIGTERM → stop accepting, `unregister_instance`, close all with 1001, clients reconnect to the new container | Step 7 |
