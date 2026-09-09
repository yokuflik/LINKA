# ADR 0033 — Rust WebSocket gateway

Status: Accepted
Date: 2026-09-10

## Context

The real-time layer is a FastAPI WebSocket endpoint (`realtime/ws_router.py` +
`realtime/connection_manager.py`), one Uvicorn process on a 1 GB t3.micro
(ADR 0007). It holds every live connection, runs JWT auth, presence/typing, the
per-connection receive loop, per-frame + per-action rate limiting, the routing
layer (`chat_instances` / `instance_inbox`), and the fan-in task that turns
`instance_inbox` pub/sub messages into client frames.

Under connection load this is the weakest link: CPython's per-connection task
overhead, the GIL serialising frame parsing / dispatch across all sockets, and
GC pauses on a box already sharing RAM with Postgres + Redis + the app itself.
The send / fan-out / receipt path is already fully async via Redis Streams
(FANOUT_REWRITE_PLAN steps 1–4) and **process-agnostic** — a fan-out published
from a Python `SendWorker` reaches whatever process owns the connection purely
through `chat_instances:{chat_id}` → `instance_inbox:{server_id}`. Nothing about
that path assumes the connection-holder is Python.

We already run one standalone Rust service (`id_service`, ADR 0011) and have the
build/deploy toolchain for it.

## Decision

Replace **only the `/ws` endpoint** with a standalone Rust service,
`ws_gateway`, that speaks the *existing* Redis contract unchanged. Python REST,
business logic, and the send / fan-out / receipt / scheduled workers are
untouched and unaware. Caddy routes `/ws` → `ws_gateway`, everything else →
the Python app.

Because the routing layer is keyed by `server_id` (a per-process UUID, not a
language), a Rust `ws_gateway` process registers in `chat_instances` exactly
like a Python process, receives fan-out on its `instance_inbox:{server_id}`
exactly like a Python process, and can run **alongside** the Python WS layer
during migration (path-based canary: `/ws` Rust, `/ws-legacy` Python).

### Cargo workspace

The repo root becomes a Cargo workspace; `id_service` moves to
`crates/id_service`. Rationale: one shared `target/` and one `Cargo.lock` so
common dependencies (`tokio`, `serde`, `prost`) compile **once** — disk and
compile-time are the binding constraints on the build host.

```
[workspace]
resolver = "2"
members  = ["crates/common", "crates/id_service", "crates/ws_gateway"]
```

`crates/common` owns the Python↔Rust seam: JWT claims + HS256 verify, Redis
key/channel/stream name builders, the wire-event `serde` enums, and wrappers for
the shared rate-limit Lua scripts. Proto codegen stays in `id_service` —
`ws_gateway` does **not** mint IDs (the Python `SendWorker` still does).

### Release profile (workspace root)

```
[profile.release]
opt-level = "z"        # WS work is I/O-bound; size beats speed
lto = "fat"
codegen-units = 1
panic = "abort"
strip = "symbols"
```

Target: ~2–4 MB static `x86_64-unknown-linux-musl` binary, ~5 MB container image
(`distroless/static` or `scratch`).

**The gateway is never compiled on the production host.** `lto=fat` +
`codegen-units=1` needs > 1 GB RAM and would OOM the t3.micro. The image is
built on the dev machine (`docker buildx --platform linux/amd64`) or in CI,
pushed to a registry, and the server only `docker pull`s — same model as
`id_service` today. A 2 GB swapfile is added to the host as a blanket backstop.
On-host fallback build knobs (`lto="thin"`, `codegen-units=16`) are documented
but not the default.

### Runtime resource budget

- `tokio` multi-thread runtime, `worker_threads = 2` (t3.micro = 2 vCPU).
- **Two Redis connections total**: one `MultiplexedConnection` for commands, one
  dedicated pub/sub connection multiplexing every subscribed channel. No pool.
  (Python opens a connection per listener; Rust does not need to.)
- System allocator (no jemalloc/mimalloc — they cost binary size + baseline RSS
  and only pay off at churn we will not see on free tier).
- Per connection: one bounded `mpsc<ServerFrame>(32)` for the write half, an
  8 KB read buffer, no growable buffers.
- Idle RSS target < 15 MB; a few hundred connections < 40 MB.

### Concurrency & state

`Arc<AppState>` with `dashmap` (sharded locks, no global `Mutex`):

| map | purpose |
|---|---|
| `conns: DashMap<ConnId, ConnHandle>` | `ConnHandle { user_id, tx, foreground: AtomicBool, last_seen: AtomicU64 }` |
| `chat_subs: DashMap<ChatId, HashSet<ConnId>>` | local routing table — mirrors Python `_chat_subscribers` |
| `user_conns: DashMap<UserId, SmallVec<ConnId>>` | multi-device |

Rules:
- **No `.await` under a DashMap guard.** Fan-out clones the `Vec<Sender>` out,
  drops the guard, then `try_send` to each. `try_send` (never `send().await`) so
  one slow client cannot stall the fan-in task; `Full` → close that client
  (1013).
- Per connection = 2 tasks: a **writer** owning the `SplitSink` (sole writer, no
  sink lock) draining the `mpsc`, and a **reader** running frame → local token
  bucket → shared Redis sliding-window Lua → dispatch.
- **One** pub/sub task subscribes `instance_inbox:{server_id}` plus dynamic
  `user_events:{uid}` / `presence_events:{uid}` via a small channel-registry
  actor on the same connection.
- Leak prevention is RAII: a drop guard removes the connection from all three
  maps; `DashMap::remove_if(set.is_empty())` reclaims empty chat keys; the
  `1→0` local-subscriber edge for a chat does `SREM chat_instances:{chat_id}`.
  A `reaper` task closes connections whose `last_seen` exceeds the heartbeat
  window.

### Redis contract (unchanged — reproduced in `crates/common`)

Outbound (Rust → Python):

| action | operation |
|---|---|
| `send_message` | `XADD message_send_stream` sharded `chat_id % SEND_STREAM_SHARDS` (shard 0 = bare key); fields incl. `client_message_id`, media columns, `enc_header` (JSON string) passed through opaquely |
| receipts | `XADD receipt_log_stream` (`enqueue_receipt_event` shape) |
| typing | replicate `realtime_service.publish_event`: `SMEMBERS chat_instances:{chat_id}` → `PUBLISH instance_inbox:{sid}` with `chat_id` injected |
| presence | `SADD presence:{uid} {sid}:{conn}` + `EXPIRE`; stamp `presence_last_seen:{uid}`; `PUBLISH presence_events:{uid}` only on the 0↔1 edge |
| routing | `SADD chat_instances:{chat_id} {server_id}` `EXPIRE 90` + reverse `instance_chats:{server_id}`; heartbeat every 30 s; `unregister_instance` on SIGTERM |

Inbound (Python → Rust): `SUBSCRIBE instance_inbox:{server_id}` →
`new_message`, receipt events, `typing`, `force_disconnect` (close 4409
silently); plus `user_events:{uid}`, `presence_events:{uid}`.

Shared enforcement: the gateway calls the **same** sliding-window and
`ws:conns:{uid}` connection-cap Lua scripts (`.claude_docs/security_and_rate_limiting.md`)
so limits stay correct across a mixed Python/Rust fleet during migration.

Handshake, before `accept()`: Origin vs `CORS_ALLOW_ORIGINS` (close 4403) → JWT
HS256 verify against the shared `JWT_SECRET` (`exp`, `sub`) → `ws_upgrade_ip`
20/10 s + `ws_upgrade_user` 10/10 s (close 4429) → connection-cap Lua (evict
oldest, publish `force_disconnect`).

### Wire compatibility

`crates/common/src/events.rs` `serde` enums must serialise byte-identically to
the Python dicts. Frozen here; guarded by a golden-JSON test — `pytest` emits
fixtures from the real Python event builders, a Rust test deserialises + round-
trips them. Any new WS action or event is a coordinated change to both sides +
this ADR.

## Consequences

- First non-Python component on the request hot path. The team now maintains a
  Rust WS codebase and a cross-language wire contract.
- The Python `realtime/ws_router.py` + `connection_manager.py` are kept dormant
  (import-guarded, mountable at `/ws-legacy`) until the Rust gateway is proven,
  then removed in a follow-up.
- `id_service` moves to `crates/id_service`; its Dockerfile and
  `docker-compose.prod.yml` service path change (mechanical).
- One shared `target/` — `crates/id_service/target/` is deleted, root `/target`
  git-ignored.
- Build moves off-host entirely (was already true for `id_service`); the host
  gains a 2 GB swapfile.
- Full step-by-step: `RUST_WS_GATEWAY_PLAN.md` (root).

## Alternatives considered

- **Keep Python, add Uvicorn workers** — the GIL still serialises frame
  dispatch; multiple workers multiply RAM on a 1 GB box.
- **Rewrite the whole app in Rust** — throws away the working REST/business
  layer for no real-time benefit; months of risk.
- **`uvloop` + PyPy** — marginal; PyPy + asyncpg/SQLAlchemy is a compatibility
  minefield.
- **Separate Rust binary, no workspace** — duplicate `target/` and dependency
  compiles blow the build-host disk budget.
