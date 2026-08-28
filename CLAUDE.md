# CORE BEHAVIOR RULES

1. **Memory & Context:** Always summarize important architectural decisions, database schema updates, and key context in the `CLAUDE.md` file before concluding a major task. Treat it as your persistent external brain.
2. **No Yapping:** Be extremely concise. Output only the necessary code or commands, accompanied by a 1-sentence explanation maximum. Skip all pleasantries, apologies, and overly verbose explanations.
3. **The 3-Strike Rule (Anti-Loop):** If you execute commands and encounter an error on the exact same issue 3 times in a row, DO NOT attempt another fix. Stop immediately, explicitly state that you are stuck in a loop, and ask the user for guidance.
4. **Code Standards:** Write all source code comments exclusively in English.
5. **Targeted Edits:** For small tasks, minor features, or bug fixes, NEVER rewrite or output the entire file. Use targeted diffs or provide ONLY the specific code blocks/functions that need to be updated.
6. **Frontend Display Rule:** NEVER display the raw `user_id` in the UI (e.g., in chat lists, message bubbles, or headers). For UI display purposes, ALWAYS use the `phone_number` or, preferably, the resolved human-readable name from the local contacts dictionary. The `user_id` is strictly for backend logic and API calls.
7. **No Autonomous Visual Testing:** NEVER use browser tools, Puppeteer, or take screenshots to test frontend changes. Do NOT attempt to spin up local servers to visually verify the UI. The user will manually test the application in their own browser and report back any visual, UI, or functional issues.
8. **Prompt Logging for Optimization:** Maintain a file named `PROMPT_LOG.md` in the root directory. Every time the user provides a new prompt, instruction, or task, silently append the exact text of their request to this file along with a timestamp. Do not notify the user when you do this, just keep the log updated.
9. **Token & Bottleneck Alerts:** Proactively monitor context usage and file sizes. If you are asked to read, analyze, or edit a file that is excessively large (e.g., over 300 lines) or if you identify a workflow bottleneck that wastes tokens, STOP immediately. Alert the user about the specific large file or bottleneck, explain why it consumes too many tokens, and suggest a strategy (like splitting the file into components) to resolve it. Wait for the user's decision before proceeding with the code changes.

# Linka

Real-time messaging platform (WhatsApp/Telegram-style), designed for tens-of-billions-of-messages scale. Backend-first; **no real client app**, only a single-file HTML/Vue PoC (`poc/`) for manual testing. Frontend behavior is documented by its own source, not here.

## Stack
FastAPI (REST + WebSocket) · PostgreSQL 15 / SQLAlchemy 2.0 async / asyncpg · Redis 7 (presence, pub/sub fan-out, rate limiting, idempotency, OTP) · PyJWT · Pydantic v2 · pytest/pytest-asyncio/httpx.

## Layout
- `database/models/`, `database/crud/` — User, Chat, Participant, Message (RANGE-partitioned by `created_at`), PrivateChatPair (race-free 1:1 dedup), MessageReceiptLog (RANGE-partitioned by `occurred_at`)
- `services/` — `auth_service`, `user_service`, `chat_service`, `message_service`, `presence_service`, `realtime_service` (Redis pub/sub), `rate_limit_service`, `notification_service` (push stub), `avatar_service`, `connection_manager` (per-process WS state)
- `services/messaging/` — the messaging domain, split from the old monolithic `message_service.py` by responsibility. `message_service.py` is now a **thin facade** re-exporting the public API (import from it as before). Modules: `errors.py` (exception types), `common.py` (`SYSTEM_MESSAGE_TYPE`, `_check_content_length`), `media_validation.py` (`_validate_media`, `MediaAttachment` — storage HEAD + per-kind limits), `send.py` (`process_outgoing`, `send_system_message`, `fan_out_message`, idempotency), `edit_delete.py` (`edit_message`, `delete_message`), `read_api.py` (`get_message_history` — attaches derived status + presigned `media_url`), `receipts.py` (`mark_as_delivered/read/played`, `get_message_receipts`, detailed-log enqueue). Tests monkeypatch `message_service.MAX_MESSAGE_CONTENT_LENGTH` / `RECEIPT_NAMED_LIST_MAX_MEMBERS` on the facade — the submodules read those back off the facade module at call time, so keep them importable there.
- `services/storage/` — object-storage subpackage, kept out of business logic (`client.py`, `media_service.py`, `errors.py`). `media_service` is the only module other services import from.
- `services/receipts/` — `receipt_log.enqueue_receipt_event` (Redis Stream) + `worker.run_forever` (one task/process, started in `main.py` lifespan)
- `services/fanout/` — the async send path (see Realtime section). Two Redis Streams / two workers, each one task/process from `main.py` lifespan: `send_queue.enqueue_outgoing_message` → `message_send_stream` → `worker.py` (persist) → `send_queue.enqueue_fanout` → `message_fanout_stream` → `fanout_worker.py` (build+publish `new_message`, push to offline). `drain_once` on each worker exposed for tests.
- `routers/` — REST (`auth`, `users`, `chats`, `messages`) + `websocket` (`/ws`); thin pass-throughs to services. Error→HTTP mapping centralized in `main.py`.
- `utils/snowflake.py` — custom Snowflake ID generator
- `scripts/` — `init_db.py` (create/drop schema, no Alembic), `init_storage.py` (buckets), `seed_mock_data.py` (5 users, chats, backdated history; re-runnable), `prune_receipt_log.py` (daily cron, DETACH+DROP old partitions)
- `tests/` — 200+ tests against real Postgres/Redis/MinIO containers, no mocks

## Running locally
```bash
docker compose up -d                                    # test_db (5433), test_redis (6380), test_minio (9100 API / 9101 console)
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" python3 -m scripts.init_db
python3 -m scripts.init_storage                          # buckets (also auto-run on app startup)
DATABASE_URL="..." REDIS_URL="redis://localhost:6380/0" uvicorn main:app --reload
```
Open `poc/index.html` directly (CORS wide open, dev-only). OTP codes print to server console — no real SMS/FCM.
`uvicorn --reload` drops every WebSocket on each `.py` save; the PoC auto-reconnects (~3s). Not a bug.

## Core architecture rules

### IDs
- Every 64-bit Snowflake id crosses the wire (REST + WS, both directions) as a **JSON string**, never a number (JS loses precision past 2^53). Never `parseInt()`/`Number()` an id in the PoC. See `routers/schemas.py`'s `IdStr`.

### Schema / migrations
- **No migrations.** Schema only via `scripts/init_db.py`. `create_all` never alters an existing table — a new column on an existing dev DB needs a manual `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (init_db does this on non-`--drop` runs) or a `--drop` re-init.
- **No automated partition management** for `messages` or `message_receipt_log`. Dev uses a single DEFAULT partition; **prod needs real monthly partitions + a creation cron**.

### Realtime / pub-sub fan-out
- **Routing layer (FANOUT_REWRITE_PLAN.md step 3, landed).** No more per-chat Redis channel. Each process registers in Redis the chats it serves (`chat_instances:{chat_id}` SET of `server_id`, TTL `CHAT_INSTANCE_TTL_SECONDS`=90, refreshed by a `_routing_heartbeat` loop every `ROUTING_HEARTBEAT_INTERVAL_SECONDS`=30; reverse map `instance_chats:{server_id}`). `realtime_service.publish_event(chat_id, event)` injects `chat_id` (str) into the event, looks up `routing.instances_for_chat`, and `PUBLISH`es once to each `instance_inbox:{server_id}` — so a group whose members sit on 3 processes costs 3 publishes, not one-per-subscribed-process. All logic in `services/fanout/routing.py`; all Redis ops best-effort (a dropped registration self-heals on next heartbeat).
- `connection_manager` now runs **one** `_instance_inbox_task` per process (lazy-start on first `connect`, cancelled on last `disconnect`) consuming `realtime_service.subscribe_to_instance_inbox(SERVER_ID)`; `_dispatch_inbox_event` routes each event by `event["chat_id"]` to `_broadcast_to_chat`. `_chat_subscribers` (chat_id → local connection_ids) is kept purely as the local routing table; `_subscribe_/_unsubscribe_connection_from_chat` call `routing.add/remove_chat_for_instance` on the 0↔1 edge (this also covers the dynamic `added_to_chat` / `removed_from_chat` paths). `main.py` shutdown calls `routing.unregister_instance(SERVER_ID)`.
- Per-connection tracking structures: local per-chat routing table + per-user channel (`user_events:{user_id}`) + per-presence-target (`presence_events:{user_id}`). The user-channel and presence listeners still start on 0→1 subscriber / stop at 0; only the chat listeners collapsed into the single inbox task.
- Per-user channel is what makes a chat/group invite created *after* a WS connected reach it live (dynamic re-subscription).
- **Sending is async (FANOUT_REWRITE_PLAN.md step 1, landed).** WS `send_message` does only a rate-limit + participant check, then `services/fanout/send_queue.enqueue_outgoing_message` (XADD `message_send_stream`) and ACKs `{"type":"ack","for":"send_message","status":"queued"}` (no `message_id`/`created_at`). `services/fanout/worker.py` (`run_forever`, one task/process, started in `main.py` lifespan; `drain_once` exposed for tests) runs the old send flow via `message_service.process_outgoing` — idempotency, `_validate_media` HEAD, `create_message`, `_fan_out`. Permanent failure (bad media / not a participant / too long) → `message_failed` on the sender's `user_events` channel + XACK; duplicate stream entry → `MessageAlreadySentError` → `message_already_sent` event + XACK; transient failure → not XACKed, XAUTOCLAIM retries. Enqueue failure is **surfaced synchronously** to the sender (`internal_error`), never swallowed like receipts.
- **Fan-out is a second hop (FANOUT_REWRITE_PLAN.md step 2, landed).** `process_outgoing` no longer calls `_fan_out` inline — after `create_message` commits it calls `send_queue.enqueue_fanout(message_id, chat_id, sender_id, client_message_id)` (XADD `message_fanout_stream`). `fanout_worker.py` drains it, loads the row, runs `message_service.fan_out_message` (was `_fan_out`, now public) — build `new_message` event, `publish_event`, push to offline. Idempotent: a redelivered entry just re-publishes (clients dedupe by `message_id`); a missing row is XACKed (nothing to do); transient failure not XACKed → XAUTOCLAIM retries. If the send worker hits `MessageAlreadySentError` (row persisted by an earlier entry that may have crashed before enqueuing fan-out) it **re-enqueues fan-out** as recovery.
- `fan_out_message` takes `client_message_id` and echoes it on the `new_message` event (never persisted) so the sender's client reconciles its optimistic bubble.
- **Read-after-write caveat:** `GET /chats` / another user won't see a message until the send worker commits (sub-second in practice). PoC is optimistic so this is invisible there; accepted tradeoff of the queue model.
- `message_service.send_message` is **gone** — call `process_outgoing` directly (tests do) or go through the queue.
- `send_system_message` **persists synchronously** (chat_service calls it inline and depends on the persisted return) but its fan-out now also goes through `enqueue_fanout` (ordering with normal messages in the same chat).
- All chat-scoped events (`new_message`, receipts, `typing`) go through `publish_event` → the routing layer → per-instance inbox channels, and every such event now carries `chat_id` (str). The sender's own connection receives its own events; clients filter themselves out.

### Presence — subscribe-on-demand
- **1:1 only.** Connect/disconnect never broadcasts online/offline. Groups never show presence.
- Ephemeral Redis (`presence:{user_id}` set + TTL, multi-device: offline only when all connections gone). `presence_last_seen:{user_id}` written only on the 0-connections edge.
- `presence_update` published only on the actual 0→1 / 1→0 edge, onto the target's own `presence_events:{user_id}` channel.
- WS: `subscribe_presence` / `unsubscribe_presence {user_id}`. Authorization = `crud_private_chat_pair.get_pair_chat_id` non-`None` (a private chat exists); self-subscribe rejected. Successful subscribe → immediate `presence_status` pull, then `presence_update` pushes.

### Auth
- `auth_service.verify_otp_and_login`'s code check (`stored_code != code`) has been found commented out during manual testing more than once — **sanity-check this line is live** before treating OTP auth as secure.

## Delivery / read / played receipts

### Watermark model (drives every tick — O(1), group-size independent)
- `MessageStatus` (SENT/DELIVERED/READ/PLAYED) is **derived, never stored on `Message`**.
- `Participant` carries per-user watermarks: `last_delivered_message_id`, `last_read_message_id`, `last_played_message_id` (+ coarse `last_*_at` TIMESTAMPTZ, never expires).
- `Chat` carries the chat-wide rollup: `all_{delivered,read,played}_up_to_message_id` = `MIN(watermark)` across current participants, maintained by `crud_participant.recompute_chat_receipt_cursors` (one query, all three).
- `compute_message_status(message_id, chat, message_type=None)` compares id vs. those columns. PLAYED only unlocked when `message_type == AUDIO_MESSAGE_TYPE` (4).
- `crud_message.create_message` bumps the sender's own three watermarks (implies having seen the chat; also prevents the sender's stale watermark pinning everyone else below a threshold forever).
- `add_participant_to_chat` seeds a new member's watermarks at the chat's current `last_message_id`, not NULL — don't remove.
- WS actions: `mark_delivered`, `mark_read`, `mark_played` (all `{chat_id, message_id}`) → `delivery_receipt`/`read_receipt`/`played_receipt` fan-out (carry `occurred_at`). `update_last_*_message` are **forward-only** — a redundant/behind re-mark returns `None` (no recompute, no event).
- Exposed: `MessageOut.status`, `ChatOut.last_message_status` (only from `chat_service.get_chat_list`; `None` from POST/PATCH chat endpoints — no watermark context).

### Detailed receipt log (separate history layer, read only by the per-message "info" view)
- **`message_receipt_log`**: append-only, RANGE-partitioned by `occurred_at`, `(id, occurred_at)` PK, no FK. Columns `chat_id, user_id, kind (2/3/4), up_to_message_id, occurred_at`. **One row per acknowledgement action**, not per (message,user) — opening a chat with 500 unread = 1 row.
- "When did U read msg X" = `occurred_at` of U's earliest row with `up_to_message_id >= X`. "Who read X in a group" = every current participant with such a row.
- **Writes async**: `mark_as_*` → `enqueue_receipt_event` → Redis Stream `receipt_log_stream` → `worker.run_forever` (`drain_once` collapses a batch to one row per (chat,user,kind) at the furthest watermark → single INSERT → `XACK`; `XAUTOCLAIM` reclaims crashed workers). Redis failure in enqueue is logged+swallowed — coarse `Participant.last_*_at` still written sync, live event still fires.
- No log row for the sender's own message ("who read X" excludes X's sender).
- Read API: `GET /chats/{chat_id}/messages/{message_id}/receipts` → `MessageReceiptsOut`. **Any participant may view any message.** `counts` always present (denominator = participants excluding sender); when not `truncated`, also `{delivered,read,played}_by` + `pending`. Group > `RECEIPT_NAMED_LIST_MAX_MEMBERS` (256) → `truncated: true`, counts only. `played_*` only for `type == 4`. Redis-cached 10s.
- Retention `RECEIPT_LOG_RETENTION_DAYS = 30`; coarse `Participant.last_*_at` is the fallback for older messages.

## Object storage (media/file attachments + avatars)

Design principle: **the app server never touches file bytes** — clients upload/download directly against storage via presigned URLs; FastAPI only mints URLs and records object keys.

- **MinIO** container `test_minio` (S3 API 9100, console 9101, creds `linka_dev`/`linka_dev_secret`).
- `config.py` `S3_*` block: endpoint/keys, `S3_BUCKET_MEDIA` (private) / `S3_BUCKET_AVATARS` (public-read), URL expiries, `MAX_UPLOAD_BYTES_*` per kind, `ALLOWED_UPLOAD_MIME` (dict by kind), `*_BY_KIND` lookups.
- `services/storage/client.py` — `signing_client()` (cached plain-boto3, **only** `generate_presigned_url`, local HMAC, safe sync from async). `async_session()`/`client_kwargs()` (aioboto3) for **every network call** (`head_object`, `delete_object`, bucket ops) — never a sync boto3 network call. `build_object_key(kind, mime)` → `{h2}/{kind}/{snowflake}{ext}` where `h2` = 2 hex of `sha1(id)` (spreads writes across storage partitions).
- `media_service.py` — `create_upload_ticket(kind, mime, size)` validates against limits, returns presigned PUT with `Content-Type` **and** `Content-Length` pinned into the signature (storage rejects a mismatched upload). `download_url(key)` = presigned GET. `public_avatar_url(key)` = plain concat, no signing. Async: `object_metadata` (HEAD, **not for the message hot path**), `object_exists`, `delete_object`, `ensure_buckets`.
- `errors.py` — `MediaValidationError` (400), `MediaNotFoundError` (404), `StorageUnavailableError` (503), mapped in `main.py`.
- `init_storage.py` auto-runs from `main.py` lifespan (warning, not crash, if unreachable).

### Media messages
- Kinds + caps: `image` 5 MB, `audio` 5 MB, `video` 20 MB, `file` 20 MB. `Message.type` 2/3/4/5 ↔ kind via `MEDIA_KIND_BY_MESSAGE_TYPE`.
- MIME whitelist (`config.ALLOWED_UPLOAD_MIME`): `image`/`video`/`audio`/`avatar` locked to known sets (they render inline). **`file` = `set()` — the "allow any non-empty MIME" sentinel**; the empty-set check is applied in **three** places (all must stay in sync): `media_service._validate_upload_request`, `message_service._validate_media`, and `routers/schemas.py`'s `MediaUploadTicketIn` validator (this last one 422s, not 400s). Size cap + `mime` non-empty still enforced everywhere. PoC's Documents picker is unfiltered; a browser reporting no `file.type` falls back to `application/octet-stream`.
- New `Message` columns (all nullable): `media_key`, `media_mime`, `media_size`, `media_name`, `media_duration_seconds`. `media_url` on `MessageOut` / `new_message` is a **presigned GET attached at read time**, not a column.
- `POST /chats/{chat_id}/messages/upload-ticket` `{kind, mime_type, size_bytes}` → ticket. Participant-only. Client PUTs bytes, then sends the WS message.
- WS `send_message`: `message_type` 2/3/4/5 + `media: {key, name?, duration_seconds?}`. `message_service._validate_media` HEADs the object and re-checks real content-type/size against per-kind limits — the client key is never trusted.
- `crud_message.build_last_message_preview(content, type)` → "📷 Photo"/"🎥 Video"/"🎤 Voice message"/"📎 File" for a caption-less media message.
- No REST send/edit/delete for messages — WebSocket-only by design.

### Avatars (user + group)
- Same direct-to-storage model. `avatars` bucket, `avatar` upload kind, same MIME/size rules. `User.profile_pic_url` / `Chat.profile_pic_url` store the **object key**, resolved to a public URL in `UserOut`/`ChatOut` `model_validator` (absolute `http(s)://` values pass through untouched — seed data).
- `avatar_service` — `set_avatar` / `set_group_avatar` / `clear_*` HEAD-verify the object, re-check limits, best-effort delete the previous object. Group variants wrapped by `chat_service` with a `ROLE_ADMIN` check + broadcast system message.
- User endpoints (`routers/users.py`, act on caller): `POST /users/me/avatar/upload-ticket`, `PUT /users/me/avatar {storage_key}`, `DELETE /users/me/avatar`.
- Group endpoints (`routers/chats.py`, admin/owner): `POST /chats/{id}/avatar/upload-ticket`, `PUT`, `DELETE`.
- `POST /chats/groups` accepts optional `avatar_storage_key` (one-shot creation-with-photo; uploads via `POST /chats/groups/avatar/upload-ticket` first, then creates). A forged/missing key aborts creation.
- `PATCH /users/me` and `PATCH /chats/{id}` **cannot** set the avatar (untrusted raw key, no cleanup).

## Chat list denormalization
- `Chat.last_message_preview` (first ~120 chars), `Chat.last_message_status`, `last_message_id`, `last_message_at` are denormalized onto `Chat` — resolving them per listed chat would hit the RANGE-partitioned `messages` with no partition to prune to.
- `last_message_preview` kept in sync on edit/delete, but only when the edited/deleted message is the chat's current `last_message_id`.
- **System messages (`sender_id = NULL`, `type = 6`) never overwrite `last_message_preview`** (`create_message` guards on `sender_id is not None`); they still bump `last_message_at`/`last_message_id`.

## Unread count
- `crud_message.count_unread_messages(session, chat_id, last_read_message_id)` — `COUNT(*) WHERE chat_id AND id > cursor AND sender_id IS NOT NULL AND deleted_at IS NULL`, uses the `ix_messages_chat_id_id` index (range scan, not full table).
- `chat_service.get_chat_list` attaches it per chat to `Participant.unread_count` (per-viewer, unlike chat-wide `last_message_status`); `GET /chats` returns `ChatListItemOut.unread_count`.

## Group membership / system messages
- Roles: 1 Member, 2 Admin, 3 Owner. `chat_service._require_role`: add/remove need `ROLE_ADMIN`, role change needs `ROLE_OWNER`. Endpoints: `POST /chats/{id}/members`, `PATCH /chats/{id}/members/{user_id}`, `DELETE /chats/{id}/members/{target_user_id}`, `GET /chats/{id}/members`.
- System message: `Message.sender_id = NULL` + `type = SYSTEM_MESSAGE_TYPE` (6), sent via `message_service.send_system_message`, fanned out over the normal `new_message` event. `chat_service._display_name_for` resolves any user named in the text (never embed a raw id).
- **`role_changed` is a private system message**: `content` is JSON (`{kind, actor_id, target_id, new_role}`) not plain text. Fan-out still reaches everyone; the client filters to `actor_id`/`target_id` only and builds the human-readable line itself. Follow this JSON-content + client-filter pattern for any future per-recipient system message (no structured metadata field on `Message`, and no migrations).

### Leaving / removing a member
- One endpoint: `DELETE /chats/{chat_id}/members/{target_user_id}` → `chat_service.remove_member`. `actor == target` is a self-leave (skips role check); otherwise actor needs `ROLE_ADMIN` **and** `target.role < actor.role` (enforced only in this function, no DB constraint).
- **Owner cannot leave a non-empty group without a `new_owner_id` query param** (validated as a member) → else `OwnershipTransferRequiredError` / HTTP 409. Successor promoted + broadcast system message before the owner's row is removed.
- **Removal leaving zero participants deletes the whole chat** (`crud_chat.delete_chat`, cascades to Participant + Message via `ON DELETE CASCADE`) — same path for "last member left" and "owner alone".
- `chat_service._notify_removed_from_chat` publishes a `removed_from_chat` personal-channel event; `connection_manager` unsubscribes that user's live connections immediately.

## Reply-to-message
- `Message.reply_to_message_id` (nullable, FK-less). Threaded through `crud_message.create_message` → `message_service.send_message(reply_to_message_id=...)` → `routers/websocket.py`. Returned on every `MessageOut` **and** on the live `new_message` fan-out event.

## Typing / recording indicator
- Fully ephemeral, no DB, no `typing_stopped` event. Client sends `{type: "typing"|"recording", chat_id}` ≤ once/3s; each receiver expires the (chat_id, user_id) pair after ~5s.
- `routers/websocket.py`'s `_handle_typing` checks `crud_participant.is_participant`, then `realtime_service.publish_event(chat_id, {event: "typing", chat_id, user_id, kind})` — reuses the per-chat Redis channel, no new pub/sub or connection_manager state. Works 1:1 and group.
- Wire event stays `"typing"`; carries a `kind` field (`"typing"` | `"recording_audio"`). Extends to other ephemeral activity kinds via the same field, no new endpoint.

## Known gaps (deliberately not built)
- No REST routes for sending/editing/deleting messages (WebSocket-only by design).
- No `call_service.py` / WebRTC — explicitly deferred.
- No DB migrations; no automated partition management (see Schema rules above).
- FANOUT_REWRITE_PLAN.md steps 1–4 all landed. Step 4: `message_send_stream` and `message_fanout_stream` are sharded by `chat_id` (`SEND_STREAM_SHARDS`/`FANOUT_STREAM_SHARDS`, default 4). `send_queue.shard_for_chat`/`stream_key` (shard 0 = bare key, upgrade-safe); each worker's `run_forever` runs one consumer task per shard; `drain_once(shard=None)` drains all shards (tests pass unchanged), `drain_once(shard=n)` drains one. One consumer group per shard.
- **Forward**: no backend support at all (no endpoint/action). **Edit/Delete**: full backend support exists (`edit_message`/`delete_message` WS actions via `message_service`) but no PoC UI trigger.

## Working conventions
- Prefer fixing real bugs found via testing over asking permission, but **flag security-relevant or destructive changes clearly** instead of silently reverting them. **Ask before backend changes** (user's standing request).
- When manually verifying against the dev Postgres container, always leave the schema created (not dropped) when done.
- **Any DB-backed test wipes the dev DB** — the suite `drop_all`s on teardown, including a single test file. Dump first (`docker exec linka-test_db-1 pg_dump -U test_user -d test_db --clean --if-exists > dump.sql`) or re-run `init_db` + `seed_mock_data` afterward. MinIO is unaffected by a Postgres wipe.
- **After editing `poc/index.html`, syntax-check its inline `<script>`** — one error (e.g. a duplicate `const`) silently kills the whole PoC. `python3 -c "import re; open('/tmp/i.js','w').write(re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>', open('poc/index.html').read(), re.S)[0])"` then `node --check /tmp/i.js`. `poc/components/*.js` and `poc/composables/*.js` can be `node --check`ed directly.
- **Never chain multiple `$emit(...)` calls with `;` in an inline template expression** — always use a real method.
- PoC frontend structure: single-file entry `poc/index.html`; template markup split into `poc/components/*.js` (`Vue.defineComponent` objects, no build step); `setup()` logic split into `poc/composables/*.js` (`useX(ctx)` factories merged onto one shared `ctx`). Plan/progress in `poc/composables/REFACTOR_PLAN.md`.
