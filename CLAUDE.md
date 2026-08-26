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

## Things worth knowing before touching auth
- Every 64-bit Snowflake id is sent as a **JSON string** on the wire (REST responses and WebSocket, both directions) — never as a JSON number. A number silently loses precision in JS past 2^53. See `routers/schemas.py`'s `IdStr` and the header comment in `poc/index.html`. Never `parseInt()`/`Number()` an id anywhere in the PoC.
- `auth_service.verify_otp_and_login`'s code-comparison check (`stored_code != code`) has been found commented-out twice during manual testing (not by Claude) — always sanity-check this line is live before treating OTP auth as secure.
- `connection_manager` uses a per-user Redis channel (`user_events:{user_id}`) so a chat created *after* a WebSocket already connected still reaches it live (dynamic re-subscription). This is what makes new chats/group invites show up without a reconnect.

## Working conventions
- Prefer fixing real bugs found via testing over asking permission, but flag security-relevant or destructive-looking changes clearly instead of silently reverting them.
- When manually verifying against the dev Postgres container, always leave the schema created (not dropped) when done — other running servers depend on it.
- The test suite runs against that same `test_db` and `drop_all`s after every test, so `pytest` wipes whatever is in the dev database. Dump it first (`docker exec linka-test_db-1 pg_dump -U test_user -d test_db --clean --if-exists > dump.sql`) if it holds anything worth keeping, or re-seed afterwards.
