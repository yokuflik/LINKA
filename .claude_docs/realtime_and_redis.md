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

- **Media send frame shape:** the PoC sends media as `{message_type: 2|3|4|5, media: {key, name?, duration_seconds?, blur_hash?}}` (nested), not the flat stream-entry field names. The Rust gateway's `SendMessageFrame` (`crates/common/src/events.rs`) maps `message_type`→`type` and the nested `media` object→flat `media_key`/`media_name`/`media_duration_seconds`/`media_blur_hash` on `SendStreamEntry`, matching the deleted Python `_handle_send_message`. A frame missing this mapping silently becomes a type-1 text message with no content/media.
- **No E2EE (ADR 0039):** `send_message` / `edit_message` carry `content` as plain text — no `enc` / `enc_header` field on the frame, the stream, or the `new_message` / `message_edited` events. `messages.content` is stored and indexed as plaintext.

## Routing layer (FANOUT_REWRITE_PLAN.md step 3, landed)
- No per-chat Redis channel. Each process registers the chats it serves: `chat_instances:{chat_id}` SET of `server_id` (TTL `CHAT_INSTANCE_TTL_SECONDS`=90, refreshed by `_routing_heartbeat` every `ROUTING_HEARTBEAT_INTERVAL_SECONDS`=30; reverse map `instance_chats:{server_id}`).
- `realtime_service.publish_event(chat_id, event)` injects `chat_id` (str) into the event, looks up `routing.instances_for_chat`, and PUBLISHes once to each `instance_inbox:{server_id}`. A group across 3 processes = 3 publishes.
- All logic in `realtime/fanout/routing.py`; all Redis ops best-effort (dropped registration self-heals on next heartbeat).
- **The consumer of `instance_inbox:{server_id}` is the Rust `ws_gateway`** (ADR 0033/0038; the Python `connection_manager` was deleted). `crates/ws_gateway/src/fanin.rs` runs one pub/sub connection: always `SUBSCRIBE instance_inbox:{server_id}`, plus dynamic `user_events:{uid}` / `presence_events:{uid}` on the 0↔1 edge. A chat-scoped message → `state.senders_for_chat(chat_id)` → `try_send`. `routing.rs` SADDs/SREMs `chat_instances` on the gateway's own local 0↔1 subscriber edge (chat ids resolved on connect via `GET /internal/ws-bootstrap`), heartbeats every 30 s, and `unregister`s on SIGTERM.
- A Python producer (`realtime_service.publish_event`) is unaware it's publishing to a Rust process — the routing layer is keyed by `server_id`, not language. The Python `main.py` still runs `routing.heartbeat` / `unregister_instance` for its own SERVER_ID (it serves zero chats now, but the reverse-map key is kept tidy).
- Per-user channel makes a chat/group invite created *after* a WS connected reach it live: `user_events:{uid}` carries `added_to_chat` / `removed_from_chat`, and the gateway adjusts `chat_subs` + `chat_instances` immediately.
- All chat-scoped events (`new_message`, receipts, `typing`) carry `chat_id` (str). Sender's own connection receives its own events; clients filter themselves out.

## Presence — subscribe-on-demand
- **1:1 only.** Connect/disconnect never broadcasts online/offline. Groups never show presence.
- **Written by the Rust gateway** (`crates/ws_gateway/src/presence.rs`) since ADR 0033/0038. `realtime/presence_service.py` is the executable spec (keys/TTLs/edges) + the live *read* side (`is_online` / `get_online_participants` / `get_status` — the fan-out worker's push-vs-live choice).
- **Foreground-only (ADR 0025).** "Online" = a *foreground* connection. WS action `presence_active {active}` → gateway `set_active` (`active` → `mark_online`, else `mark_offline`). `presence:{user_id}` = SET of **bare connection ids** (matches `presence_service.py`; NOT the `{sid}:{conn}` `ws:conns` form), TTL `PRESENCE_TTL_SECONDS`=60. Online iff ≥1. Connect `mark_online`s (assumed foreground); client sends `presence_active {false}` if it opened hidden, re-asserts on reconnect. `heartbeat` refreshes the TTL — **and the client only pings it while the tab is foreground** (`useWebsocket.js`), so a frozen/backgrounded tab drops offline within 60 s even if the explicit `presence_active:false` was missed. Metered under `ws_sub_presence`; client de-dupes.
- `presence_last_seen:{user_id}` (RFC3339, no TTL) re-stamped on every mark_online / heartbeat / mark_offline. `presence_update` published on the 0↔1 edge only, onto `presence_events:{user_id}`.
- WS: `subscribe_presence` / `unsubscribe_presence {user_id}`. The gateway authorises via **`GET /internal/presence-authorized?watcher_id=&target_user_id=`** (`realtime/presence_authz.presence_authorized`): `nobody` → deny; `contacts` → `crud_private_chat_pair.get_pair_chat_id` non-`None`; `everyone` (default) → any authed user. Self-subscribe → `error`/`bad_request` (gateway-local). Allowed → `presence_status` pull (gateway reads the `presence_service`-shaped keys directly), then `presence_update` pushes.
- **The gate runs only at subscribe time.** The client re-sends `subscribe_presence` on every heartbeat (~30 s); the gateway re-calls `/internal/presence-authorized`, and a now-disallowed watcher gets `{type: "presence_revoked", user_id}` + its `presence_events:{uid}` subscription dropped. Worst-case staleness ≈ one heartbeat.

## Typing / recording indicator
- Fully ephemeral, no DB, no `typing_stopped` event. Client sends `{type: "typing"|"recording", chat_id}` ≤ once/3s; each receiver expires the (chat_id, user_id) pair after ~5s. **Client only emits while the tab is physically foreground** (ADR 0025 — `useChatOpen.windowIsActive()`, same rule as `mark_read`).
- The gateway gates every typing/recording frame via **`GET /internal/typing-allowed?chat_id=&sender_id=`** (one call per event, fail-closed), then replicates `publish_event(chat_id, {event: "typing", chat_id, user_id, kind})` (`SMEMBERS chat_instances` → PUBLISH per instance).
- **`/internal/typing-allowed`** (`realtime/internal_router.py`) = participant check + the 1:1 privacy gate: if the chat has exactly 2 participants and the sender's `privacy.online` doesn't let the other see them (`presence_authorized(watcher=other, target=sender)`), returns `{allowed: false}` → the gateway silently drops the frame. Checked per event (a privacy change applies immediately). Groups (>2) never gated. A short-TTL local participant cache in the gateway is a deferred optimisation.
- Wire event stays `"typing"`; carries `kind` (`"typing"` | `"recording_audio"`). Extend via the same field, no new endpoint.

## Receipts — fully async (ADR 0037)
- **The whole receipt side effect is behind `receipt_log_stream` now**, like `send_message`. WS handlers (`_handle_mark_*` in `ws_router.py` **and** the Rust gateway's `mark_*`) do only `XADD receipt_log_stream` (fields `chat_id,user_id,kind,up_to_message_id,occurred_at`) + ack. No DB session, no publish on the WS path. `modules/messaging/receipts.mark_as_{delivered,read,played}` collapsed to that single enqueue (`session` arg kept, unused).
- `modules/receipts/worker.run_forever` → `receipt_log.drain_once` → **`modules/receipts/apply.py::apply_receipt`** per collapsed (chat,user,kind) at furthest watermark:
  1. `update_last_{delivered,read,played}_message` — coarse watermark (drives unread). No-op if already past.
  2. `played` only: skip unless the target message is a voice message (the check that used to raise `NotAVoiceMessageError` to the client — now a **silent drop**).
  3. only if the watermark advanced: write the `message_receipt_log` row **and** publish the live `delivery_receipt`/`read_receipt`/`played_receipt` — the latter two suppressed when the reader hid their receipts in a 1:1 (ADR 0003, evaluated in the worker).
  Batch collapse + XAUTOCLAIM reclaim unchanged. Transient failure → batch not XACKed, redelivered (apply is idempotent).
- **Consequence:** receipts are eventually-consistent (worker latency, sub-second); the `mark_*` ack means "queued", not "applied". Redis failure in enqueue is logged+swallowed (client re-sends on next scroll).

## WebSocket limits — enforced by the Rust gateway (ADR 0033/0038)

Same Redis-backed limits, same keys/windows (COMMS_SECURITY_PLAN steps 5–6),
moved from the deleted `realtime/ws_router.py` / `ws_connection_registry.py`
into `crates/ws_gateway` (`ws.rs` / `handlers.rs` / `message_ops.rs`, calling
the **verbatim** Lua in `linka-common::ratelimit`). Mixed-fleet-safe if a Python
process ever runs alongside.

- **Connection cap** — `ZSET ws:conns:{user_id}`, member `{server_id}:{connection_id}`,
  cap `WS_CONN_MAX_CONNECTIONS`=5, **evict oldest** via one Lua on connect
  (`ZREMRANGEBYSCORE` past `WS_CONN_MAX_AGE_SECONDS`=26 h → `ZADD` → `ZPOPMIN` while
  over → `PEXPIRE`). Evicted members → `PUBLISH instance_inbox:{sid}`
  `{"event":"force_disconnect","connection_id":…}`; the owning gateway's `fanin`
  fires that conn's cancel-notify → reader queues a **4409** close. `ZREM` on disconnect.
- **Handshake churn** — `ws_upgrade_ip` 20/10 s + `ws_upgrade_user` 10/10 s, after
  JWT verify, before the socket is accepted. Over → close **4429**.
- **Inbound frame rate** — `ws_frame` 30/10 s per connection before dispatch; over →
  `rate_limited` + drop the frame; `WS_FRAME_FLOOD_STRIKES` consecutive → close 4429.
- **`send_message`** — two-tier `send_message` 3/1 s + `send_message_burst` 40/60 s.
- **Per-action** — `ws_receipts` 60/10 s (`mark_*`), `ws_typing` 10/10 s, `ws_sub_presence`
  20/10 s (`subscribe_presence` + `presence_active`), `ws_edit` 20/60 s
  (`edit_message`/`delete_message`/`restore_message`/`purge_message`, relayed to
  `/internal/message/*`). `heartbeat` / `unsubscribe_presence` unmetered.
- **Origin** — checked against `CORS_ALLOW_ORIGINS` pre-upgrade; missing/foreign → **4403** (CSWSH).
- Chat-id resolution on connect: `GET /internal/ws-bootstrap` (`get_all_chat_ids_for_user`,
  `WS_MAX_CHAT_IDS_ON_CONNECT`=2000 ceiling).

Full limit table + close codes: `.claude_docs/security_and_rate_limiting.md`.

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

## Rust WS gateway (ADR 0033/0038 — the only `/ws`, LIVE since 2026-09-10)
`crates/ws_gateway` (Cargo workspace crate alongside `id_service`) serves `/ws`. It speaks the Redis contract of the deleted Python layer unchanged — registers in `chat_instances:{chat_id}` (`routing.rs`), consumes `instance_inbox:{server_id}` + `user_events:{uid}` + `presence_events:{uid}` on one shared pub/sub connection (`fanin.rs`), XADDs `message_send_stream`/`receipt_log_stream`, replicates `publish_event`, calls the **verbatim** rate-limit + `ws:conns` Lua (`linka-common::ratelimit`). Python send/fan-out/receipt/scheduled workers are untouched and unaware. The **Python↔Rust seam is `realtime/internal_router.py`** (`/internal/*`, ADR 0036) — edge-blocked (Caddy `respond /internal* 404`), reached only at `http://app:8000` on the compose net.
- **Connect (ADR 0036):** `GET /internal/ws-bootstrap?token=` → `{user_id, chat_ids}` (JWT-verified, same `get_all_chat_ids_for_user`). Populates `chat_subs`, SADDs `chat_instances` per 0→1 edge. Failure → connect with an empty chat set (self-heals on reconnect).
- **Presence + typing:** `presence.rs` mirrors `presence_service.py`; `subscribe_presence` / typing gate via `GET /internal/presence-authorized` / `GET /internal/typing-allowed` (see the Presence / Typing sections above).
- **Receipts (ADR 0037):** `mark_*` → `XADD receipt_log_stream` (`receipts.rs`) + ack; the Python `receipt_log` worker does watermark + ADR 0003 mask + live event.
- **Message mutations (ADR 0038):** `edit_message`/`delete_message`/`restore_message`/`purge_message` → `message_ops.rs` → `POST /internal/message/{edit,delete,restore,purge}` (`ws_edit` bucket) → `message_service.*` (own fan-out). Ack on 2xx, `forbidden`/`bad_request`/`internal_error` on 403/400/else.
- **Deploy:** musl→distroless ~2.2 MB, built off-host (`docker buildx --platform linux/amd64 --load` + `docker save | ssh | docker load`), tag `linka-ws-gateway:latest`. Caddy `/ws` + `/ws-legacy` → `ws_gateway:8081`. `ALLOWED_HOSTS` auto-includes `app`/`localhost` (`main.py`) for the internal calls. See `.claude_docs/deployment.md`.
Full plan: `RUST_WS_GATEWAY_PLAN.md`.

## App-liveness gate for send_message / receipts (ADR 0041)
- **The gap:** `/ws` is served entirely by the Rust gateway (ADR 0033/0038), so stopping only the Python app (uvicorn killed, `app` container down) leaves the socket correctly "connected" — that part is by design. But `send_message` is a plain `XADD` onto `message_send_stream` with an immediate `queued` ack, and the only consumer is the Python app's in-process `send_worker` task (started in `main.py`'s `lifespan`, same for fan-out/receipt workers). With the app down, nothing drains the stream, yet the gateway kept acking `queued` — a message would sit stuck forever with no error, though it *looked* sent.
- **The fix:** `realtime/fanout/base_worker.py::touch_app_liveness()` — `SET app_worker_alive:{SERVER_ID} 1 EX APP_LIVENESS_TTL_SECONDS` (default 10s) — called every `BaseStreamConsumer._run_shard` loop iteration (send worker, fan-out worker) and every `modules/receipts/worker.run_forever` iteration. One key per app process (single-process deploy per ADR 0007, so in practice one key).
- The gateway checks `EXISTS app_worker_alive:{app_server_id}` (`send_path::app_workers_alive`, `crates/ws_gateway/src/send_path.rs`) before enqueueing `send_message` and before enqueueing any `mark_*` receipt (`ws.rs`'s `mark` helper) — missing/expired (or the `EXISTS` call itself failing) → `{"type":"error","code":"internal_error", ...}` instead of a false ack, fail-closed.
- Gateway config: `APP_SERVER_ID` env — must equal the app's own `SERVER_ID` (prod: both `linka-1` from `.env`, wired in `docker-compose.prod.yml`; local dev: set both to the same value, e.g. `linka-dev` — see CLAUDE.md's "Running locally"). A mismatch makes every send/receipt look like the app is permanently down.
- `edit_message`/`delete_message`/`restore_message`/`purge_message` were already correctly erroring (`internal_error`) in this scenario since ADR 0038 relays them synchronously to `/internal/message/*` — no change needed there. `typing`/`presence_active`/`subscribe_presence`/`ws-bootstrap` stay best-effort/self-healing by design (ADR 0036) — not gated.
- **Frontend needed no changes**: `poc/composables/useOutbox.js` already treats `internal_error` as retryable (exponential backoff, bubble stays on 🕓), and its existing 90s `ACK_TIMEOUT_MS` safety net already flags the bubble ⚠️ ("not sent") if retries never resolve — this fix makes that existing indicator fire correctly instead of the bubble staying falsely "sent"/pending forever.

## Local dev note
`uvicorn --reload` drops every WebSocket on each `.py` save; the PoC auto-reconnects (~3s). Not a bug.
