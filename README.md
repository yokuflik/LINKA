# Linka

A real-time messaging platform (WhatsApp / Telegram–style) built backend-first around two problems most chat-app tutorials skip: operating at scale, and running an autonomous AI agent as a first-class actor inside the messaging system rather than as a bolted-on chatbot widget.

There is no production client — only a single-file Vue 3 proof-of-concept (`poc/`) for manual testing. The real product is the backend: a feature-based modular monolith fronted by a standalone Rust WebSocket gateway, with async Redis-stream fan-out, a partitioned Postgres core, and dual server-side search (full-text + semantic vector) — all exercised by ~700 integration tests against real Postgres/Redis/MinIO.

---

## AI Capabilities — built in, not bolted on

Every Linka user can provision a personal **AI agent** that acts as their own messaging service account — it has its own identity, sends and receives real messages through the exact same send/fan-out/receipt pipeline a human uses, and is bound by the same per-user rate limits, restrictions and DB triggers as any other participant. It is not a chat window pointed at an LLM; it is a peer of the human user inside the core message model (`messages.sender_agent_id`, DB-level restriction triggers, shared WebSocket send budget).

**Two decoupled models, two different jobs:**

- **Gemini (tool-calling reasoning engine)** — the agent's actual "brain." Runs full multi-round-trip function-calling turns against a **12-tool execution-mode registry** (send/reply, create/leave chats, read history, keyword + semantic search over messages, semantic search over its own knowledge base, escalate to a human) and a separate **config-mode registry** for the owner's own setup conversation (persona/rules/triggers/schedules, resolved through a Supervisor → Builder → Help state machine). BYOK supported — an owner can supply their own Gemini key instead of the shared one.
- **TypeSafe `jev` (structured intent classifier)** — a dedicated, non-generative classification model that judges every inbound customer message *before* the main agent turn ever runs, on four atomic dimensions (on-topic, prompt-injection, confidential-info extraction, code-execution). Off-topic messages get a redirect (a lightweight Gemini call authors only the one-sentence reply, in the customer's own language); anything flagging as malicious auto-escalates to the human owner with a deterministic, audited reason — with zero extra classification calls, since the same `jev` call already produced the verdict.

**What the agent actually does, autonomously:**

- Wakes on configurable triggers — time windows, specific chats with keyword gates, first-message-from-a-stranger, every message in any private chat, or a cron-style schedule — debounced and coalesced so a burst of fast messages produces exactly one coherent turn, not a race of several.
- Retrieves grounding data via **Agentic RAG**: a per-agent knowledge base (uploaded documents or text the agent decides on its own is reference data, never re-injected into the prompt verbatim) is chunked, embedded, and semantically searched — the same pgvector infrastructure the platform's own message search uses, scoped per agent.
- Enforces hard limits on itself and is enforced *again* independently at the Postgres layer: `restrictions` JSONB (can it message groups? new contacts? how many sends/day?) is checked in Python and re-checked by dedicated `BEFORE INSERT/DELETE` triggers, so a bug in a tool handler can never bypass what the owner configured.
- Tracks its own resource consumption — Gemini calls/minute, function round-trips/turn, daily active-processing-time, and two rolling token-usage windows (5h / 7d) — surfaced to the owner as a live usage dashboard in the PoC, with pre-flight budget checks and graceful in-place retries before ever silently going dark.
- Can pause a conversation and page a human, with an owner-facing notice written by the model in natural language and a customer-facing handoff message, all without either side ever seeing an internal id or a hint that a judge/model runs underneath.

---

## Architecture & Scale

This is not "a Node app with a Postgres table" — it is a distributed system with a real service boundary between hot-path transport and business logic:

- **Standalone Rust WebSocket gateway** (`crates/ws_gateway`) is the *only* thing serving `/ws` in production — it owns connection lifecycle, presence, typing indicators, per-connection rate limiting, and WS-frame-level backpressure/gap-detection, while the Python app stays reachable only through a locked-down internal REST seam (`/internal/*`, edge-blocked at the proxy). A separate Rust gRPC service mints Snowflake IDs under load with a lock-free atomic CAS loop, unary-only by design (batching would stale the id's embedded timestamp, which doubles as the Postgres partition-routing key).
- **Feature-based modular monolith** in Python (`infra/` → cross-cutting primitives with zero domain logic, `realtime/` → pub/sub + presence + the Rust seam, `modules/<feature>/` → messaging, chats, media, search, agents, each a self-contained package with its own models/CRUD/service/router). No layer-cake `services/`/`routers/`/`database/` split — every feature owns its full vertical slice.
- **Fully async, multi-hop send pipeline.** A `send_message` frame only rate-limits and authorizes, then `XADD`s onto a `chat_id`-sharded Redis Stream and returns `queued` immediately — persistence and fan-out happen in separate consumer-group workers, so write latency is entirely decoupled from delivery latency. Routing to the right socket uses a registry of which process currently serves which chat (`chat_instances:{chat_id}`), not a channel-per-chat pub/sub, so a group's fan-out is O(processes-serving-it), not O(members).
- **Search happens on the server, not in the client — two engines, for two different questions.** Exact/boolean keyword search runs as native Postgres full-text search (`tsvector` + a trigger-maintained `GIN` index) for "find the message that said X." A separate **pgvector-backed semantic search** — Gemini embeddings, cosine distance, an IVFFlat index built only after there's real data to cluster — answers "find the message that *meant* this," across a person's own chats or scoped inside the agent's private knowledge base. Both enforce chat membership at the SQL level via a join, never via a pre-fetched id list, so access is always current.
- **Time-partitioned Postgres at message-log scale.** `messages` is weekly-range-partitioned, the detailed receipt-acknowledgement log is daily-partitioned, both maintained by an idempotent script on a committed cron schedule (create-ahead, freeze-when-cold, prune, migrate an overflow DEFAULT partition online). Every read path decodes the Snowflake id back into a `created_at` bound first, so Postgres prunes whole partitions instead of scanning history — the difference between an unread-count query touching one partition versus all of them.
- **Receipts and presence are watermark-based, not per-message rows.** Read/delivered/played state is *derived* (a per-participant watermark id, rolled up to a per-chat MIN across members) — an O(1) operation independent of group size — with an append-only log kept separately only to answer "who specifically has read this," on demand, with its own short retention and partition lifecycle.
- **Defense in depth everywhere it matters**: transport hardening and per-identity rate limiting live in Redis (never in-process, since the deployment target is inherently multi-process/multi-worker); the AI agent's restrictions are checked twice, once in application code and once by the database itself.

## Tech Stack

| Layer | Technology |
|---|---|
| API / Realtime edge | FastAPI (REST) + standalone Rust `ws_gateway` (the only `/ws`) |
| Backend language | Python 3.13, fully async |
| Database | PostgreSQL 15 + `pgvector`, range-partitioned tables |
| ORM / driver | SQLAlchemy 2.0 (async) / asyncpg |
| Cache, streams, pub/sub | Redis 7 (Streams for fan-out/receipts, sliding/fixed-window rate limits, presence, routing) |
| Object storage | S3-compatible — MinIO in dev/CI, AWS S3 in production |
| AI — reasoning | Google Gemini, function/tool calling |
| AI — safety classification | TypeSafe `jev`, structured intent classification |
| AI — embeddings | Gemini embeddings + pgvector (IVFFlat, cosine) |
| ID generation | 64-bit Snowflake, in-process or via a standalone Rust gRPC service under load |
| Auth | Firebase Phone Auth (manual RS256 JWKS verification) + PyJWT access/refresh |
| Frontend (PoC only) | Vue 3 + Tailwind, single HTML file, no build step |
| Testing | pytest / pytest-asyncio / httpx against real Postgres, Redis and MinIO — no mocks |
| Deployment | Docker Compose, single-host demo topology behind Caddy |

## Repository Layout

```text
infra/          Cross-cutting primitives — db, redis, ids, rate limiting. No domain logic.
realtime/       Pub/sub fan-out, presence, notifications, the Rust gateway's internal seam.
modules/        One package per feature — messaging, chats, media, search, vector_search, agents,
                users, settings, auth — each owning its own models/crud/service/router.
api/            Shared request/response primitives (e.g. the Snowflake-id-as-string type).
crates/         Rust workspace: ws_gateway (the /ws server) + shared common crate.
id_service/     Standalone Rust gRPC Snowflake ID service.
scripts/        Schema/storage init, partition maintenance, search/vector backfills, seeding.
poc/            Single-file Vue 3 manual-testing client — not a product.
deploy/         Caddy config, prod Postgres tuning, cron schedules, env template.
docs/adr/       Numbered Architecture Decision Records — every material design decision, dated.
tests/          ~700 integration tests against real backing services (ephemeral per-run database).
```

## Running Locally

```bash
docker compose up -d                                    # Postgres :5433, Redis :6380, MinIO :9100/:9101
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" python3 -m scripts.init_db
python3 -m scripts.init_storage
./run_dev.sh   # uvicorn + the AI agent worker + the Rust ws_gateway, together, with a shared shutdown trap
```

Open `poc/index.html` directly — OTP codes print to the server console (no real SMS in dev). Interactive API docs at `http://localhost:8000/docs`.

The test suite creates and drops its own throwaway database per run — it never touches your seeded dev data.

## Design Record

Every material architectural, schema, or infrastructure decision is captured as a dated, numbered **ADR** in `docs/adr/` — from the original fan-out design through the AI agent's tool-mode gating, the Rust gateway migration, and the judge/escalation pipeline. It's the fastest way to see *why* the system looks the way it does, not just what it looks like today.

**Deliberately out of scope:** a production client app (PoC only), voice/video calls, a DB migration framework (schema is managed idempotently by script), and horizontal scale-out of the current single-host demo deployment (the architecture supports it; the demo doesn't exercise it).
