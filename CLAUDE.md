# CORE BEHAVIOR RULES

1. **Memory & Context:** Always summarize important architectural decisions, database schema updates, and key context in the `CLAUDE.md` file before concluding a major task. Treat it as your persistent external brain.
2. **No Yapping:** Be extremely concise. Output only the necessary code or commands, accompanied by a 1-sentence explanation maximum. Skip all pleasantries, apologies, and overly verbose explanations.
3. **The 3-Strike Rule (Anti-Loop):** If you execute commands and encounter an error on the exact same issue 3 times in a row, DO NOT attempt another fix. Stop immediately, explicitly state that you are stuck in a loop, and ask the user for guidance.
4. **Code Standards:** Write all source code comments exclusively in English.
5. **Targeted Edits:** For small tasks, minor features, or bug fixes, NEVER rewrite or output the entire file. Use targeted diffs or provide ONLY the specific code blocks/functions that need to be updated.

# Linka

A real-time messaging platform (WhatsApp/Telegram-style), designed for tens-of-billions-of-messages scale. Built incrementally, backend-first — **there is no real client app**, only a single-file HTML/Vue PoC for manual testing.

## Stack
FastAPI (REST + WebSocket) · PostgreSQL 15 via SQLAlchemy 2.0 async/asyncpg · Redis 7 (presence, pub/sub fan-out, rate limiting, idempotency, OTP) · PyJWT · Pydantic v2 · pytest/pytest-asyncio/httpx for tests.

## Layout
- `database/models/`, `database/crud/` — User, Chat, Participant, Message (RANGE-partitioned by `created_at`), PrivateChatPair (race-free 1:1 chat dedup)
- `services/` — business logic: `auth_service`, `user_service`, `chat_service`, `message_service`, `presence_service`, `realtime_service` (Redis pub/sub), `rate_limit_service`, `notification_service` (push stub), `connection_manager` (per-process WebSocket state)
- `routers/` — REST (`auth`, `users`, `chats`, `messages`) + `websocket` (`/ws`), thin pass-throughs to services; error→HTTP mapping centralized in `main.py`
- `poc/index.html` — single-file Vue 3 + Tailwind (both via CDN) test client, no build step
- `utils/snowflake.py` — custom Snowflake ID generator
- `scripts/init_db.py` — creates/drops schema (no Alembic migrations exist); `scripts/seed_mock_data.py` — fills it with 5 users, private/group chats between them and a backdated message history (safe to re-run)
- `tests/` — 197+ tests, all against real Postgres/Redis containers (no mocks)

## Running locally
```bash
docker compose up -d                                    # test_db (5433), test_redis (6380)
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" python3 -m scripts.init_db
DATABASE_URL="..." REDIS_URL="redis://localhost:6380/0" uvicorn main:app --reload
```
Open `poc/index.html` directly in a browser (CORS is wide open, dev-only). OTP codes print to the server console (`[STUB] Would SMS OTP ...`) — no real SMS/FCM provider is wired up.

**`uvicorn --reload` drops every open WebSocket on each file save** — if testing live and something edits a `.py` file, expect the PoC's connection to drop and auto-reconnect (~3s). Not a bug.

## Known gaps (deliberately not built)
- No REST routes for sending/editing/deleting messages — that's WebSocket-only by design.
- No `call_service.py` / `media_service.py` (WebRTC, S3 uploads) — explicitly deferred.
- No DB migrations — schema only via `scripts/init_db.py`; no automated partition management for `messages`. `create_all` never alters an existing table, so a new column on an existing dev DB needs a manual `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (or a `--drop` + re-init, which loses the data).
- `message_service._fan_out` is synchronous with `send_message` (not queued) — a deliberate call, not yet revisited (see fan-out decision in memory).
- **Contact Discovery was requested and abandoned mid-spec, nothing built**: a `DeviceContactsProvider` frontend service mocking a device-contacts API (phone numbers `"1"`-`"10"`), a `POST /contacts/sync` backend endpoint to match them against registered users, and a "My Contacts" UI section with a "Start Chat" button. The request was interrupted before any code was written. Don't assume any of it exists.

## Things worth knowing about the chat list
- `Chat.last_message_preview` (first ~120 chars of the last message's content) and `Chat.last_message_status` (see receipts below) are both denormalized onto `Chat` for the same reason as `last_message_id`/`last_message_at`: resolving them from `messages` per listed chat would hit a RANGE-partitioned table with no partition to prune to. `last_message_preview` is kept in sync on edit/delete too (`crud_message.edit_message_content`/`soft_delete_message` rewrite/clear it, but only when the edited/deleted message is actually the chat's current `last_message_id`).
- The PoC's contact "names" (`MOCK_CONTACT_NAMES` in `poc/index.html`, e.g. phone `"1"` → "Daniel Cohen") are a **client-only lookup with nothing behind it on the server** - it fires whenever a phone number/id happens to be the literal string `"1"`-`"10"`, regardless of that user's real `display_name`. `scripts/seed_mock_data.py`'s `CONTACT_NAMES` dict is a duplicate of the same ten names/numbers, kept manually in sync, so the seeded data actually exercises the mapping (five real registered users with phone numbers `"1"`-`"10"`, each with a private chat to every one of the five main `MOCK_USERS`). Don't be surprised to find users with plain single/double-digit phone numbers in the dev DB - they're intentional.

## Things worth knowing about delivery/read receipts
- `MessageStatus` (`database/models/message.py`: SENT/DELIVERED/READ — WhatsApp's one grey / two grey / two blue checks) is **derived, never stored on `Message`**. Storing it directly would mean writing to that row every time it crosses a threshold — in a group, that threshold is "every one of up to ~1000 participants," on a table sized for tens of billions of rows.
- Instead, `Participant` carries two per-user watermarks — `last_read_message_id` (pre-existing) and `last_delivered_message_id` (mirrors it) — and `Chat` carries their chat-wide rollup: `all_delivered_up_to_message_id` / `all_read_up_to_message_id`, each the `MIN(...)` of that watermark across the chat's current participants ("the highest message id literally everyone has delivered/read"). `crud_message.compute_message_status(message_id, chat)` then just compares a message's id against those two chat columns — O(1), independent of group size or history length.
- `crud_participant.recompute_chat_receipt_cursors` is what maintains those chat-wide columns, called after every `update_last_delivered_message`/`update_last_read_message`/`remove_participant`, and from `crud_message.create_message` (sending implies having seen the chat up to that point, which auto-bumps the sender's own watermarks and can itself unstick a cursor that was stuck on the sender's stale one).
- `add_participant_to_chat` seeds a new participant's watermarks at the chat's *current* `last_message_id`, not `NULL`/0 — otherwise adding one new member to an already fully-read 1000-person group would reset both cursors to "nothing," making years of history report as unread again. Don't remove that seeding.
- WebSocket actions: `mark_delivered` and `mark_read` (both `{chat_id, message_id}`), each firing a `delivery_receipt`/`read_receipt` fan-out event. The `new_message` fan-out event also carries `status` (always `1`/SENT at that instant - the cursors can't already cover an id that didn't exist a moment ago).
- Exposed over REST: `MessageOut.status` (`GET /chats/{id}/messages`, always attached by `message_service.get_message_history`) and `ChatOut.last_message_status` (only ever attached by `chat_service.get_chat_list` - the `POST`/`PATCH` chat endpoints that also return a `ChatOut` don't have participant-watermark context loaded, so it's `None` there, not a guess).
- The PoC (`poc/index.html`) renders the tick (✓ / grey ✓✓ / blue ✓✓) under each of *your own* sent messages, and auto-sends receipts: `mark_delivered` the instant a `new_message` event reaches the socket (regardless of which chat is open - that's the point of "delivered"), `mark_read` additionally when that chat is the one on screen, and both for the newest message whenever a chat is opened (`selectChat`, to catch up on anything missed). On a `delivery_receipt`/`read_receipt` event for the open chat, it re-fetches that page's statuses (`refreshMessageStatuses`) rather than guessing client-side which of the sender's own messages actually changed - a single event only tells you one participant's watermark, not whether that made this chat's aggregate cross a threshold.

## Things worth knowing before touching auth
- Every 64-bit Snowflake id is sent as a **JSON string** on the wire (REST responses and WebSocket, both directions) — never as a JSON number. A number silently loses precision in JS past 2^53. See `routers/schemas.py`'s `IdStr` and the header comment in `poc/index.html`. Never `parseInt()`/`Number()` an id anywhere in the PoC.
- `auth_service.verify_otp_and_login`'s code-comparison check (`stored_code != code`) has been found commented-out twice during manual testing (not by Claude) — always sanity-check this line is live before treating OTP auth as secure.
- `connection_manager` uses a per-user Redis channel (`user_events:{user_id}`) so a chat created *after* a WebSocket already connected still reaches it live (dynamic re-subscription). This is what makes new chats/group invites show up without a reconnect.

## Working conventions
- Prefer fixing real bugs found via testing over asking permission, but flag security-relevant or destructive-looking changes clearly instead of silently reverting them.
- When manually verifying against the dev Postgres container, always leave the schema created (not dropped) when done — other running servers depend on it.
- The test suite runs against that same `test_db` and `drop_all`s after every test, so `pytest` wipes whatever is in the dev database. Dump it first (`docker exec linka-test_db-1 pg_dump -U test_user -d test_db --clean --if-exists > dump.sql`) if it holds anything worth keeping, or re-seed afterwards.
