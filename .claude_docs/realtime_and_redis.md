# Realtime, Pub/Sub Fan-out & Redis

Read this before touching WebSocket handling, the send/fan-out path, presence, typing, or any Redis-backed coordination.

Design rationale & trade-offs: `docs/adr/0001-redis-pubsub-fanout-routing.md`.

## Redis usage overview
Redis 7 is used for: presence, pub/sub fan-out routing, rate limiting, idempotency, OTP, and the async send/fan-out/receipt Streams.

## Async send path (FANOUT_REWRITE_PLAN.md steps 1–4, all landed)
- WS `send_message` does only a rate-limit + participant check, then `realtime/fanout/send_queue.enqueue_outgoing_message` (XADD `message_send_stream`) and ACKs `{"type":"ack","for":"send_message","status":"queued"}` (no `message_id`/`created_at`).
- `realtime/fanout/worker.py` (`run_forever`, one task/process, started in `main.py` lifespan; `drain_once` exposed for tests) runs the old send flow via `message_service.process_outgoing` — idempotency, `_validate_media` HEAD, `create_message`, then `send_queue.enqueue_fanout`.
  - Permanent failure (bad media / not a participant / too long) → `message_failed` on the sender's `user_events` channel + XACK.
  - Duplicate stream entry → `MessageAlreadySentError` → `message_already_sent` event + XACK, **and re-enqueue fan-out** as recovery.
  - Transient failure → not XACKed, XAUTOCLAIM retries.
  - Enqueue failure is surfaced synchronously to the sender (`internal_error`), never swallowed.
- **Fan-out is a second hop.** `process_outgoing` calls `send_queue.enqueue_fanout(message_id, chat_id, sender_id, client_message_id)` (XADD `message_fanout_stream`). `fanout_worker.py` drains it, loads the row, runs `message_service.fan_out_message` (was `_fan_out`, now public) — build `new_message` event, `publish_event`, push to offline. Idempotent (redelivered → re-publish, clients dedupe by `message_id`; missing row → XACK).
- `fan_out_message` takes `client_message_id` and echoes it on `new_message` (never persisted) so the sender's client reconciles its optimistic bubble.
- `message_service.send_message` is **gone** — call `process_outgoing` directly (tests do) or go through the queue.
- `send_system_message` **persists synchronously** (chat_service depends on the persisted return) but its fan-out also goes through `enqueue_fanout` (ordering with normal messages).
- **Read-after-write caveat:** `GET /chats` won't see a message until the send worker commits (sub-second). PoC is optimistic so it's invisible there.
- **Step 4 sharding:** `message_send_stream` and `message_fanout_stream` are sharded by `chat_id` (`SEND_STREAM_SHARDS`/`FANOUT_STREAM_SHARDS`, default 4). `send_queue.shard_for_chat`/`stream_key` (shard 0 = bare key, upgrade-safe). Each worker's `run_forever` runs one consumer task per shard; `drain_once(shard=None)` drains all shards, `drain_once(shard=n)` one. One consumer group per shard.

- **E2E (ADR 0026):** `send_message` may carry `enc` (opaque header dict); when present `content` is base64 ciphertext. Carried as a JSON string field `enc_header` on `message_send_stream`, parsed back by `SendWorker._rebuild_enc_header`, stored in `messages.enc_header` and echoed on `new_message` (`is_encrypted` + `enc_header`). Server never decrypts.

## Routing layer (FANOUT_REWRITE_PLAN.md step 3, landed)
- No per-chat Redis channel. Each process registers the chats it serves: `chat_instances:{chat_id}` SET of `server_id` (TTL `CHAT_INSTANCE_TTL_SECONDS`=90, refreshed by `_routing_heartbeat` every `ROUTING_HEARTBEAT_INTERVAL_SECONDS`=30; reverse map `instance_chats:{server_id}`).
- `realtime_service.publish_event(chat_id, event)` injects `chat_id` (str) into the event, looks up `routing.instances_for_chat`, and PUBLISHes once to each `instance_inbox:{server_id}`. A group across 3 processes = 3 publishes.
- All logic in `realtime/fanout/routing.py`; all Redis ops best-effort (dropped registration self-heals on next heartbeat).
- `connection_manager` runs **one** `_instance_inbox_task` per process (lazy-start on first `connect`, cancelled on last `disconnect`) consuming `realtime_service.subscribe_to_instance_inbox(SERVER_ID)`; `_dispatch_inbox_event` routes by `event["chat_id"]` to `_broadcast_to_chat`.
- `_chat_subscribers` (chat_id → local connection_ids) is the local routing table; `_subscribe_/_unsubscribe_connection_from_chat` call `routing.add/remove_chat_for_instance` on the 0↔1 edge (covers dynamic `added_to_chat` / `removed_from_chat`).
- `main.py` shutdown calls `routing.unregister_instance(SERVER_ID)`.
- Per-connection tracking: local per-chat routing table + per-user channel (`user_events:{user_id}`) + per-presence-target (`presence_events:{user_id}`). User-channel and presence listeners start on 0→1 / stop at 0; only chat listeners collapsed into the single inbox task.
- Per-user channel makes a chat/group invite created *after* a WS connected reach it live (dynamic re-subscription).
- All chat-scoped events (`new_message`, receipts, `typing`) carry `chat_id` (str). Sender's own connection receives its own events; clients filter themselves out.

## Presence — subscribe-on-demand
- **1:1 only.** Connect/disconnect never broadcasts online/offline. Groups never show presence.
- **Foreground-only (ADR 0025).** "Online" = a *foreground* connection. WS action `presence_active {active: bool}` → `presence_service.set_active(user_id, connection_id, SERVER_ID, active)` (thin wrapper: `active` → `mark_online`, else `mark_offline` — reuses the edge-triggered publish + multi-device set + last_seen stamp). The `presence:{user_id}` set now means "foreground connections"; online iff ≥1. Connect still `mark_online`s (assumed foreground); the client sends `presence_active {false}` if it opened hidden and re-asserts on every reconnect. `heartbeat` still refreshes the TTL for whatever's in the set (a backgrounded, `srem`-ed connection's heartbeat is a harmless no-op). Rate-limited under the shared `ws_sub_presence` bucket; client de-dupes (sends only on an actual change). `is_online`/`get_online_participants` now also mean "foreground" → a backgrounded user gets a push, not a silent live fan-out.
- Ephemeral Redis (`presence:{user_id}` set + TTL, multi-device: offline only when all connections gone). `presence_last_seen:{user_id}` (ISO string, no TTL) = **the moment of the most recently connected device**: `_touch_last_seen` re-stamps it "now" on every `mark_online`, every `heartbeat`, and every `mark_offline` (any device dropping, not just the 0-edge), so it is already current the instant the last device goes. `get_status` returns it always; while `status == "online"` the client ignores it.
- `presence_update` published only on the actual 0→1 / 1→0 edge, onto the target's own `presence_events:{user_id}` channel.
- WS: `subscribe_presence` / `unsubscribe_presence {user_id}`. Authorization = target user's `privacy.online` setting (`services/settings`, via `_presence_authorized` in `realtime/ws_router.py`): `nobody` → deny; `contacts` → `crud_private_chat_pair.get_pair_chat_id` must be non-`None`; `everyone` (default) → any authed user. Self-subscribe → `error`/`bad_request`. Allowed → `presence_status` pull, then `presence_update` pushes.
- **The gate runs only at subscribe time, never per push** (a user with thousands of watchers must not cost a DB check per connect/disconnect). To pick up a later privacy change without a per-push check or a revoke fan-out: the client re-sends `subscribe_presence` for its open chat on every heartbeat (~30s); `_handle_subscribe_presence` re-runs `_presence_authorized`, and if the watcher is no longer allowed it calls `connection_manager.unsubscribe_presence` and sends `{type: "presence_revoked", user_id}`. Client (`usePresence.onPresenceRevoked`) drops the cached status. Worst-case staleness ≈ one heartbeat interval.

## Typing / recording indicator
- Fully ephemeral, no DB, no `typing_stopped` event. Client sends `{type: "typing"|"recording", chat_id}` ≤ once/3s; each receiver expires the (chat_id, user_id) pair after ~5s. **Client only emits while the tab is physically foreground** (ADR 0025 — `useChatOpen.windowIsActive()`, same rule as `mark_read`); server-side unchanged.
- `realtime/ws_router.py`'s `_publish_typing` loads `get_chat_participants`, checks the sender is one, then `realtime_service.publish_event(chat_id, {event: "typing", chat_id, user_id, kind})`.
- **Privacy (1:1 only):** the typing/recording indicator is a presence-like signal, so it follows the **sender's own** `privacy.online`. If the chat has exactly 2 participants and the sender's `privacy.online` doesn't let the other participant see the sender online (`_presence_authorized(watcher_id=other, target_user_id=sender)`), the indicator is **silently dropped** (no event, no error). The sender's setting only ever restricts what the sender emits — it never affects what the sender receives. Checked live on every event — unlike the presence gate (heartbeat re-check), so a privacy change applies immediately. Groups (>2 participants) are never gated.
- Wire event stays `"typing"`; carries `kind` (`"typing"` | `"recording_audio"`). Extend via the same field, no new endpoint.

## Receipts — fully async (ADR 0037)
- **The whole receipt side effect is behind `receipt_log_stream` now**, like `send_message`. WS handlers (`_handle_mark_*` in `ws_router.py` **and** the Rust gateway's `mark_*`) do only `XADD receipt_log_stream` (fields `chat_id,user_id,kind,up_to_message_id,occurred_at`) + ack. No DB session, no publish on the WS path. `modules/messaging/receipts.mark_as_{delivered,read,played}` collapsed to that single enqueue (`session` arg kept, unused).
- `modules/receipts/worker.run_forever` → `receipt_log.drain_once` → **`modules/receipts/apply.py::apply_receipt`** per collapsed (chat,user,kind) at furthest watermark:
  1. `update_last_{delivered,read,played}_message` — coarse watermark (drives unread). No-op if already past.
  2. `played` only: skip unless the target message is a voice message (the check that used to raise `NotAVoiceMessageError` to the client — now a **silent drop**).
  3. only if the watermark advanced: write the `message_receipt_log` row **and** publish the live `delivery_receipt`/`read_receipt`/`played_receipt` — the latter two suppressed when the reader hid their receipts in a 1:1 (ADR 0003, evaluated in the worker).
  Batch collapse + XAUTOCLAIM reclaim unchanged. Transient failure → batch not XACKed, redelivered (apply is idempotent).
- **Consequence:** receipts are eventually-consistent (worker latency, sub-second); the `mark_*` ack means "queued", not "applied". Redis failure in enqueue is logged+swallowed (client re-sends on next scroll).

## WebSocket connection cap + handshake churn (COMMS_SECURITY_PLAN step 5 — DONE)

- **`realtime/ws_connection_registry.py`** — cross-process cap `WS_CONN_MAX_CONNECTIONS`
  (5) concurrent connections / user, **evict the oldest** (never reject the new one).
  `ZSET ws:conns:{user_id}`, member `"{server_id}:{connection_id}"`, score = connect
  epoch-ms. One `register_script` Lua on connect (after auth + `accept()`):
  `ZREMRANGEBYSCORE` older than `WS_CONN_MAX_AGE_SECONDS` (26h, crash-leak sweep) →
  `ZADD` self → while `ZCARD > cap`: `ZPOPMIN` and collect → `PEXPIRE` (skipped if
  max-age ≤ 0). Returns the evicted members. All Redis ops best-effort (failure →
  logs, returns `[]`, connection proceeds).
- `realtime/ws_router.py`: after `register`, for each evicted `"{server_id}:{connection_id}"`
  → `realtime_service.publish_to_instance(server_id, {"event":"force_disconnect","connection_id":…,"reason":"connection_limit"})`.
  `unregister` in the endpoint `finally` (alongside `disconnect` + `mark_offline`).
- `connection_manager._dispatch_inbox_event`: a `force_disconnect` event (no `chat_id`)
  → `_handle_force_disconnect(connection_id)` — **closes `4409` silently** (business
  answer 4: no `disconnected` frame, no banner) then `disconnect()` for local-table
  consistency (both idempotent; the endpoint's `finally` also cleans up). N parallel
  opens converge to exactly the newest `cap` (one Lua script).
- **Handshake churn**: `realtime/ws_router.py` checks `check_sliding_window` per IP
  (`ws_upgrade_ip`, `WS_UPGRADE_IP_RATE_LIMIT_MAX`=20 / 10 s) and per user
  (`ws_upgrade_user`, 10 / 10 s) **right after auth, before `accept()`** and before
  the per-connect DB query. Over → close `4429`, no accept.
- `get_all_chat_ids_for_user(session, user_id, limit=WS_MAX_CHAT_IDS_ON_CONNECT)` —
  2000-row defensive ceiling on the connect query (not pagination).

## WS per-frame + per-action limits (COMMS_SECURITY_PLAN step 6 — DONE)

- **Inbound frame rate**: `realtime/ws_router.py` receive loop, per `connection_id`,
  `check_sliding_window("ws_frame", 30, 10)` **before** `_dispatch`. Over →
  `rate_limited` + **drop the frame** (no dispatch, no close). A local strike
  counter (reset on any passing frame) closes `4429` at `WS_FRAME_FLOOD_STRIKES`.
- **`send_message`**: `_handle_send_message` two-tier sliding check — `send_message`
  3/1 s **and** `send_message_burst` 40/60 s.
- **Per-action**: `_ACTION_LIMITS` table consulted in `_dispatch` before the
  handler (`mark_*`→`ws_receipts` 60/10 s; `edit`/`delete`/`restore`→`ws_edit`
  20/60 s; `typing`/`recording`→`ws_typing` 10/10 s; `subscribe_presence` +
  `presence_active`→`ws_sub_presence` 20/10 s). Shared `action_key` = shared
  bucket. `heartbeat` / `unsubscribe_presence` unmetered.
Full limit table: `.claude_docs/security_and_rate_limiting.md`.

**Already live (step 3):** `/ws` checks the `Origin` header against
`CORS_ALLOW_ORIGINS` **before** the token check and `accept()` — a missing or
foreign Origin closes `4403` (CSWSH). `["*"]` (dev default) allows any Origin.

## Scheduled messages (ADR 0031)
- Redis key **`scheduled_messages:due`** — ZSET, member = `scheduled_messages.id`, score = `scheduled_for` epoch seconds. `ZADD` on create, `ZREM` on cancel, re-`ZADD` on reschedule. It is a fast index only — **Postgres is the source of truth**.
- **`realtime/fanout/scheduled_worker.py`** — one poll-based task per process (NOT a stream consumer — volume is tiny), started in `main.py` lifespan next to `send_task`/`fanout_task` (added to the cancel/await list). `run_forever(stop_event)`, plus `drain_once(*, batch=…)` and `reconcile_once()` for tests — both open their own `session_scope()`, no session arg.
  - Every `SCHEDULED_POLL_INTERVAL_SECONDS` (5): `ZRANGEBYSCORE scheduled_messages:due -inf <now> LIMIT 0 SCHEDULED_WORKER_BATCH` (100).
  - Per id: `ZREM` (claim) → `get_scheduled_for_update` (`FOR UPDATE SKIP LOCKED`), bail unless `status=0` → re-check `is_participant(chat_id, sender_id)` (else `set_status(3, "no longer a participant")` + `scheduled_message_failed`) → `send_queue.enqueue_outgoing_message(...)` **with the stored `client_message_id`** → `set_status(1)` + `scheduled_message_sent`. `_fire_one` writes with `commit=False`; `drain_once` commits the per-id `session_scope` transaction.
  - Transient failure (exception out of `_fire_one`) → `bump_fire_attempts`; below `SCHEDULED_MAX_FIRE_ATTEMPTS` (5) re-`ZADD` score `now + SCHEDULED_FIRE_BACKOFF_SECONDS` (30), else `set_status(3, "fire failed after retries")` + `scheduled_message_failed`. The row's `fire_attempts` SMALLINT column tracks this.
- **Reconcile**: on worker startup and every `SCHEDULED_RECONCILE_INTERVAL_SECONDS` (60): `SELECT id, scheduled_for FROM scheduled_messages WHERE status=0` → re-`ZADD` all. Self-heals a Redis flush, a missed add, or a fire time that elapsed during downtime (overdue = "due" → fires next poll).
- **Delivery re-uses the live send path**: `send_queue.enqueue_outgoing_message` is the same entry point `_handle_send_message` uses → identical idempotency, `_validate_media` HEAD, fan-out, receipts, offline push, ordering. Scheduled sends bypass only the WS per-user limiter (server-originated); the send stream + `send_message_burst` window still pace fan-out.
- Exactly-once under a 2-process race: `ZREM` claim + `FOR UPDATE SKIP LOCKED` + reused-`client_message_id` idempotency in `process_outgoing` (`MessageAlreadySentError`).
- **User-channel events** on `user_events:{sender_id}` (via `realtime_service.publish_user_event`, same mechanism as `chat_pin_changed`), emitted by the worker: `scheduled_message_sent {id, chat_id}` (no `message_id` — the real send is async, its id isn't known yet; the client removes the row and lets the normal `new_message` echo render the bubble), `scheduled_message_failed {id, chat_id, reason}`. The `scheduled_message_changed` multi-device REST-handler event is NOT yet implemented (deferred to the frontend step).

## Rust WS gateway (ADR 0033 — LIVE in production since 2026-09-10; next: ADR 0038 deletes the Python layer)
The FastAPI `/ws` endpoint is being replaced by a standalone Rust service (`ws_gateway`, a Cargo workspace crate under `crates/` alongside `id_service`). It speaks **this exact Redis contract unchanged** — registers in `chat_instances:{chat_id}` (`crates/ws_gateway/src/routing.rs`), consumes `instance_inbox:{server_id}` + `user_events:{uid}` + `presence_events:{uid}` on one shared pub/sub connection (`fanin.rs`), XADDs `message_send_stream`/`receipt_log_stream`, replicates `publish_event`, calls the same rate-limit + `ws:conns` Lua. Python send/fan-out/receipt/scheduled workers are untouched and unaware; a Rust process is indistinguishable from a Python one to the routing layer, so both can run at once (canary: `/ws` Rust, `/ws-legacy` Python).
- **Connect bootstrap (ADR 0036):** on connect the gateway calls `GET /internal/ws-bootstrap?token=` on the Python app (`realtime/internal_router.py`) to resolve the user's chat ids (same `get_all_chat_ids_for_user`), then populates its local `chat_subs` and SADDs `chat_instances` on each 0→1 edge. `/internal*` is JWT-verified **and must 404 at the edge** (Caddy rule, plan Step 7).
- **Presence + typing (Step 6) landed on the gateway:** `presence.rs` mirrors `presence_service.py` (connect/heartbeat/`presence_active` → `presence:{uid}` bare-conn-id SET + TTL `PRESENCE_TTL_SECONDS`=60 + edge `presence_update` on `presence_events:{uid}`); `subscribe_presence` authorises via `GET /internal/presence-authorized` (shared rule `realtime/presence_authz.py`, also used by `ws_router`), dynamically subscribes the pub/sub channel, pulls `presence_status`; typing/recording gate via `GET /internal/typing-allowed` (participant + 1:1 privacy, one call **per typing event**, fail-closed), then `publish_event(chat_id, {event:"typing",kind,user_id})`.
- **Receipts (ADR 0037):** the gateway's `mark_delivered`/`mark_read`/`mark_played` `XADD receipt_log_stream` (`crates/ws_gateway/src/receipts.rs`) + bucket + ack; the Python `receipt_log` worker does the rest for both fleets (see "Receipts — fully async" above).
- **Message mutations (ADR 0038):** `edit_message`/`delete_message`/`restore_message`/`purge_message` (WS-only, no REST) are relayed by `crates/ws_gateway/src/message_ops.rs` → `POST /internal/message/{edit,delete,restore,purge}` (`ws_edit` bucket) → wraps `message_service.*` (which fan out `message_edited`/`_deleted`/`_restored`/`_purged` themselves). Gateway relays ack on 2xx, `forbidden` on 403, `bad_request` on 400, `internal_error` otherwise.
- **Legacy Python `/ws`:** mounted iff `LEGACY_WS_ENABLED` (default true — `config/app_settings.py`); Caddy serves it at `/ws-legacy`. Set false + delete the layer (ADR 0038) once the gateway is canaried. **Every WS action the client sends is now handled by the gateway.**
Full plan: `RUST_WS_GATEWAY_PLAN.md`.

## Local dev note
`uvicorn --reload` drops every WebSocket on each `.py` save; the PoC auto-reconnects (~3s). Not a bug.
