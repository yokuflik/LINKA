# Linka 🚀

A real-time messaging platform (WhatsApp / Telegram style) designed around the architectural challenges of tens-of-billions-of-messages workloads. 

**Linka** is a backend-first project demonstrating advanced system design, asynchronous message processing, and time-partitioned data storage. While it includes a Vue 3 proof-of-concept client for manual testing, the core focus is a production-ready, highly concurrent backend architecture.

### 🌟 Key Capabilities
* **Massive Concurrency:** Decoupled write and delivery pipelines ensure ultra-low latency even under extreme load.
* **Smart Storage:** $O(1)$ read receipts and automated PostgreSQL time-partitioning keep queries fast as data grows.
* **Optimized Bandwidth:** Direct-to-storage media uploads with content-addressed deduplication (SHA-256).

**Live Demo:** [linka-web.com](https://linka-web.com) *(Running on a single t3.micro EC2 instance — please be gentle)*  
*(📸 Tip: Add a short GIF here demonstrating a real-time chat with read receipts)*

---

### 🏗 Architecture Overview

```mermaid
graph TD
    Client[Vue 3 Client] -->|HTTPS / WSS| Caddy[Caddy Reverse Proxy]
    Caddy -->|REST & WebSocket| FastAPI[FastAPI App]
    FastAPI -->|Write & Query| PG[(PostgreSQL 15)]
    FastAPI -->|Pub/Sub & Streams| Redis[(Redis 7)]
    FastAPI -->|Presigned URLs| S3[MinIO / AWS S3]
    Client -.->|Direct Upload via Presigned URL| S3
    
    subgraph Async Pipeline
    Redis -.->|Event Stream| Workers[Background Workers]
    Workers -.->|Route to Sockets| FastAPI
    end
```

### ⚙️ Technical Highlights

* **Asynchronous Send Pipeline:** A WebSocket `send_message` only rate-limits and authorizes, then `XADD`s to a `chat_id`-sharded Redis Stream and returns `{"status": "queued"}`. Dedicated worker tasks persist the row and fan the message out, entirely decoupling write latency from delivery.
* **Pub/Sub Routing Layer:** Instead of a channel per chat, each process registers the chats it currently serves in `chat_instances:{chat_id}`. A `publish_event` resolves the serving processes and publishes once to each `instance_inbox:{server_id}`. One inbox-consumer task per process routes events to local sockets.
* **$O(1)$ Read Receipts:** Tick state (SENT / DELIVERED / READ / PLAYED) is derived, never stored. Each Participant holds per-user watermarks; each Chat holds the MIN-across-participants rollup. Cost is entirely independent of group size. An append-only `message_receipt_log` backs the per-message "info" view only.
* **Time-Partitioned Postgres:** Messages live in weekly partitions, and `message_receipt_log` in daily partitions, managed by a standalone idempotent Python script on a committed crontab. Snowflake IDs are decoded into a `created_at` predicate so Postgres prunes whole partitions on history reads.
* **Direct-to-Storage Media:** The app server never touches file bytes. Clients request a presigned PUT (with Content-Type and Content-Length pinned), upload directly to S3/MinIO, then send the message; the server HEADs the object and re-validates the real MIME type and size. Content-addressed dedup (`sha256`) skips re-uploading known blobs.
* **Privacy-Aware Presence & Receipts:** Presence/typing is 1:1 only (groups never leak it), subscribe-on-demand, and authorized against the target's `privacy.online` setting. In 1:1, READ/PLAYED acknowledgements from a peer who disabled read receipts are masked to DELIVERED on sender-facing surfaces (watermarks still advance — the mask is presentation-only).

### 🛠 Technology Stack

| Area | Technology |
| :--- | :--- |
| **Web Framework** | FastAPI 0.115 (REST + WebSocket in one app) |
| **ASGI Server** | Uvicorn 0.34 (single process in production) |
| **Database** | PostgreSQL 15, RANGE-partitioned tables, JSONB settings |
| **ORM / Driver** | SQLAlchemy 2.0 async / asyncpg |
| **Cache & Pub/Sub** | Redis 7 (Presence, routing, rate limiting, OTP, Streams) |
| **Auth** | PyJWT (HS256) + Firebase Phone Auth (RS256 ID-token verification) |
| **Object Storage** | MinIO (dev) / AWS S3 (prod), aioboto3 + boto3 |
| **ID Generation** | 64-bit Snowflake (Optional Rust gRPC ID service under load) |
| **Frontend PoC** | Vue 3 + Tailwind (CDN, no build step) |

### 📂 Repository Layout

```text
main.py                  FastAPI assembly, background worker lifespan
config/                  Settings package (env-var driven)
routers/                 Thin HTTP + WebSocket entry points
services/                Core business logic (messaging, fanout, receipts, storage, rate-limiting)
database/                SQLAlchemy models and CRUD operations
scripts/                 init_db, init_storage, seed_mock_data, partition_maintenance
id_service/              Optional Rust gRPC Snowflake ID service (ADR 0011)
poc/                     Single-file Vue 3 client
deploy/                  Caddyfile, prod Postgres config, env examples
docs/adr/                21 Architecture Decision Records
```

### 🚀 Running Locally

Requires **Docker** and **Python 3.13**.

```bash
# 1. Start backing services (Postgres :5433, Redis :6380, MinIO :9100/:9101)
docker compose up -d

# 2. Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Create schema, partitions, and storage buckets
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" python3 -m scripts.init_db
python3 -m scripts.init_storage

# 4. (Optional) Seed demo data (5 users, chats, backdated history)
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" python3 -m scripts.seed_mock_data

# 5. Run the API
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" REDIS_URL="redis://localhost:6380/0" uvicorn main:app --reload
```
Open `poc/index.html` directly in a browser. Interactive API docs are available at `http://localhost:8000/docs`. *(Note: Phone strings 1–5 are a dev whitelist that skips SMS verification).*

### 🧪 Tests
The repository includes ~200 integration tests run against real Postgres, Redis, and MinIO containers (no mocks).
> **Warning:** DB-backed tests execute `drop_all` on teardown and will clear your local dev database. Re-run `init_db` (and optionally `seed_mock_data`) afterwards. MinIO is unaffected.

### 📐 Design Notes & Out of Scope
Every significant architectural and infrastructure decision is documented as a numbered **ADR** in `docs/adr/`. 

**Deliberately out of scope for this project:**
* Production client app (only PoC provided).
* Calls / WebRTC.
* DB Migration tool (Schema managed natively by `init_db.py`).
* Horizontal scaling deployment (Architecture supports it; current demo deployment does not exercise it).