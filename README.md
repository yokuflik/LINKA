# Linka

A real-time messaging platform (WhatsApp / Telegram style) engineered for a **tens-of-billions-of-messages** scale.
Backend-first: the repository ships a complete FastAPI backend plus a single-file Vue 3 proof-of-concept client used for manual testing — there is no production mobile/desktop app.

**Live demo:** https://16-171-249-95.sslip.io/ (a single `t3.micro` EC2 instance — please be gentle).

---

## Highlights

- **Asynchronous send pipeline** — a WebSocket `send_message` only rate-limits and authorizes, then `XADD`s to a `chat_id`-sharded Redis Stream and returns `{"status": "queued"}`. Dedicated worker tasks persist the row and fan the message out. Write latency is decoupled from delivery.
- **Pub/sub routing layer, not a channel per chat** — each process registers the chats it currently serves in `chat_instances:{chat_id}`; `publish_event` resolves the serving processes and publishes once to each `instance_inbox:{server_id}`. One inbox-consumer task per process routes events to local sockets.
- **O(1) read receipts** — tick state (`SENT / DELIVERED / READ / PLAYED`) is *derived*, never stored. Each `Participant` holds per-user watermarks; each `Chat` holds the MIN-across-participants rollup. Cost is independent of group size. An append-only `message_receipt_log` (written asynchronously via a Redis Stream + batch-collapsing worker) backs the per-message "info" view only.
- **Time-partitioned Postgres** — `messages` in weekly partitions, `message_receipt_log` in daily partitions (e.g. `messages_y2026w07`), managed by a standalone idempotent Python script on a committed crontab (no `pg_partman`). Snowflake IDs are decoded into a `created_at` predicate so Postgres prunes whole partitions on history reads.
- **Direct-to-storage media** — the app server never touches file bytes. The client requests a presigned `PUT` (with `Content-Type` and `Content-Length` pinned), uploads directly to S3/MinIO, then sends the message; the server `HEAD`s the object and re-validates the real MIME type and size. Content-addressed dedup (`sha256`) means a known blob skips the upload entirely.
- **Real phone verification without `firebase-admin`** — Firebase JS SDK performs the SMS + reCAPTCHA client-side; the server verifies the resulting RS256 ID token manually against Google's cached JWKS, then find-or-creates the user and issues its own JWTs.
- **Privacy-aware presence & receipts** — presence/typing is 1:1 only (groups never leak it), subscribe-on-demand, and authorized against the target's `privacy.online` setting. In 1:1, `READ`/`PLAYED` acknowledgements from a peer who disabled read receipts are masked to `DELIVERED` on sender-facing surfaces (watermarks still advance — the mask is presentation-only).
- **~200 integration tests** against real Postgres / Redis / MinIO containers — no mocks.

---

## Technology stack

| Area | Technology |
|---|---|
| Web framework | FastAPI 0.115 (REST + WebSocket in one app) |
| ASGI server | Uvicorn 0.34 (single process in production) |
| Database | PostgreSQL 15, RANGE-partitioned tables, JSONB settings |
| ORM / driver | SQLAlchemy 2.0 async / asyncpg |
| Cache & coordination | Redis 7 — presence, pub/sub routing, rate limiting, idempotency, OTP, Streams |
| Auth | PyJWT (HS256 access/refresh) + Firebase Phone Auth (RS256 ID-token verification) |
| Validation | Pydantic v2 |
| Object storage | MinIO (dev) / AWS S3 (prod), `aioboto3` + `boto3` for URL signing |
| ID generation | 64-bit Snowflake — always crosses the wire as a JSON string. Optional Rust gRPC ID service under load (`id_service/`) |
| Testing | pytest / pytest-asyncio / httpx against real containers |
| Frontend PoC | Vue 3 + Tailwind, both from CDN, no build step |
| Deployment | Docker Compose + Caddy (TLS + reverse proxy) on a single host |

---

## Repository layout

```
main.py                  FastAPI assembly, centralized exception→HTTP mapping,
                         lifespan that starts every background worker
config/                  Settings package — 8 domain sub-modules behind a
                         `from .<sub> import *` facade (env-var driven)

routers/                 Thin HTTP + WebSocket entry points
  auth.py                OTP dev stub, Firebase verify, refresh, logout
  users.py               Profile, username lookup/availability, avatar, settings
  chats.py               Private/group lifecycle, membership & roles, pin, mute,
                         media & avatar upload tickets, receipts view
  messages.py            Message history read (WebSocket owns writes by design)
  websocket.py           /ws — send, delivered/read/played, edit/delete/restore/
                         purge, typing/recording, presence subscribe

services/                Business logic
  messaging/             Send pipeline, edit/delete, history read, receipts,
                         media validation, read-receipt privacy masking
  fanout/                Redis-Streams send queue, send & fan-out workers,
                         routing registry + heartbeats
  receipts/              Detailed receipt-log stream + batch-collapsing worker
  settings/              Per-user JSONB settings (sparse, patch-validated)
  storage/               Presigned-URL client, upload tickets, HEAD/exists/delete
  chat_service.py        Facade re-exporting services/chats/*
  presence_service.py    Online status + last-seen, ephemeral, multi-device
  realtime_service.py    publish_event(chat_id, event)
  connection_manager.py  Per-process socket state + inbox consumer task
  rate_limit_service.py  Redis fixed + sliding-window limiter
  firebase_auth.py       Manual RS256 verification against Google JWKS

database/
  models/                User, Chat, Participant, Message (partitioned),
                         PrivateChatPair, MessageReceiptLog (partitioned),
                         UserSettings, MediaBlob
  crud/                  One module per aggregate
                         No migration tool — schema via scripts/init_db.py,
                         new columns via ALTER TABLE ... ADD COLUMN IF NOT EXISTS

scripts/                 init_db, init_storage, seed_mock_data,
                         manage_partitions, partition_maintenance (cron entry),
                         prune_receipt_log

id_service/              Optional Rust gRPC Snowflake ID service (ADR 0011)
poc/                     Single-file Vue 3 client — index.html + components/*.js
                         + composables/*.js (useX(ctx) factories)
deploy/                  Caddyfile, prod Postgres config, env example, crontabs
docs/adr/                21 Architecture Decision Records
.claude_docs/            Deep-dive domain documentation
```

---

## Running locally

Requires Docker and Python 3.13.

```bash
# 1. Start backing services (Postgres :5433, Redis :6380, MinIO :9100/:9101)
docker compose up -d

# 2. Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Create the schema + partitions and the storage buckets
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" \
  python3 -m scripts.init_db
python3 -m scripts.init_storage

# 4. (optional) Seed demo data — 5 users, chats, backdated history
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" \
  python3 -m scripts.seed_mock_data

# 5. Run the API
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" \
  REDIS_URL="redis://localhost:6380/0" \
  uvicorn main:app --reload
```

Then open `poc/index.html` directly in a browser. OTP codes print to the server console — there is no real SMS locally. Phone strings `1`–`5` are a dev whitelist that skips verification entirely.

Interactive API docs: http://localhost:8000/docs

### Tests

```bash
pytest
```

> **Warning:** any DB-backed test runs `drop_all` on teardown and will wipe your local dev database. Re-run `init_db` (+ `seed_mock_data`) afterwards. MinIO is unaffected.

---

## Deployment

A single free-tier EC2 box (1 vCPU, 1 GB RAM + swap) running everything through `docker-compose.prod.yml`:

```
caddy (80/443)   TLS + reverse proxy + static PoC
  └── app        uvicorn, 1 worker = one full set of lifespan background workers
  ├── db         postgres:15-alpine
  └── redis      7-alpine, 128 MB maxmemory, appendonly
```

Production object storage is real AWS S3. A cron job on exactly one host runs partition maintenance and a nightly `pg_dump`. Full runbook: [`deploy/README.md`](deploy/README.md).

---

## Design notes

Every significant architectural, schema, or infrastructure decision is recorded as a numbered ADR in [`docs/adr/`](docs/adr/) — Redis routing, JSONB settings, read-receipt privacy, time-partition management, single-host deploy, Firebase auth, content-addressed media dedup, the Rust ID service, transport hardening & rate limiting, username identity, and more.

### Deliberately out of scope

- No production client app (only the PoC).
- No REST routes for send/edit/delete — WebSocket-only by design.
- No calls / WebRTC.
- No migration tool (Alembic) — schema is managed by `init_db.py`.
- No storage-object lifecycle deletion.
- Not highly available or horizontally scaled — a deliberate demo constraint; the architecture is built to allow it, the deployment does not exercise it.

---

## License

No license is currently specified. All rights reserved by the author.
