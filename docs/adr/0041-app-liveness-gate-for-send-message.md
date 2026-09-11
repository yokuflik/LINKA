# 0041 — App-liveness gate for `send_message` (and receipts)

Status: Accepted

## Context

The Rust `ws_gateway` is the sole `/ws` endpoint (ADR 0033/0038). It has no
DB and no business logic of its own for the async send path: `send_message`
is a plain `XADD` onto the sharded `message_send_stream`
(`crates/ws_gateway/src/send_path.rs`), and the gateway immediately acks
`{"type":"ack","for":"send_message","status":"queued"}` regardless of
whether anything is actually going to drain that stream.

The only thing that drains `message_send_stream` is the Python app's
`send_worker.run_forever` task, started in `main.py`'s `lifespan` and run
**in-process with uvicorn** (same for the fan-out worker and the receipt
worker, ADR 0037). When only the Python app is stopped (e.g. `uvicorn`
killed for a local restart, or the `app` container crashes) while the Rust
gateway keeps running:

- The socket correctly stays open — `/ws` is served by Rust, not Python, so
  this is by design (ADR 0033/0038), not a bug.
- `send_message` still enqueues onto Redis and still acks `queued` — Redis
  itself doesn't care whether a consumer is running.
- Nothing ever drains the entry until the Python app comes back, so the
  message sits in the stream indefinitely with no error surfaced to the
  sender. The client shows the bubble as sent; it never actually sends.
- The same silent-stall applies to `mark_delivered`/`read`/`played`
  (`receipt_log_stream`, ADR 0037).

This is worse than the already-accepted best-effort failures elsewhere
(`ws-bootstrap`, `presence-authorized`, `typing-allowed` — all self-heal on
reconnect and only affect liveness/typing, not message delivery
correctness).

## Decision

Add a cheap Redis liveness signal the send-path workers refresh while
running, and have the gateway check it synchronously before acking
`send_message` — a missing/expired key means "nothing is draining the
stream right now," so the gateway returns `internal_error` immediately
instead of a false `queued`.

- **Key:** `app_worker_alive:{SERVER_ID}` (string, no fixed value needed —
  `SET ... EX`). One key per app process, mirroring how `chat_instances`/
  `instance_chats` are already keyed by `SERVER_ID` (ADR 0001). Deployed as
  a single Python app process (ADR 0007), so in practice there is exactly
  one key, but per-process keying keeps this correct if that ever changes
  and costs nothing extra.
- **Refreshed by:** `BaseStreamConsumer._run_shard`'s existing loop
  (`realtime/fanout/base_worker.py`) — every iteration (whether or not it
  drained anything) does a best-effort `SET app_worker_alive:{SERVER_ID} 1
  EX <ttl>`. This covers the send worker, fan-out worker, and receipt
  worker for free since they all subclass `BaseStreamConsumer`; no new task.
- **TTL:** `APP_LIVENESS_TTL_SECONDS` = 10s. The loop iterates continuously
  (blocking `XREADGROUP` with `block_ms` on empty reads, `SEND_WORKER_BLOCK_MS`
  order of ~1-2s), so a live worker refreshes the key several times per TTL
  window; a stopped process lets it expire within one TTL window (worst case
  ~10s of "looks alive" after a hard kill — acceptable, matches the existing
  60s presence TTL order of magnitude and is far tighter).
- **Checked by:** the gateway, once per `send_message` and once per
  `mark_*`, via a plain `EXISTS app_worker_alive:{app_server_id}` — the
  gateway is told the Python app's `SERVER_ID` via config
  (`APP_SERVER_ID` env, matching the app's own `SERVER_ID`; single-process
  deploy so this is static, not looked up dynamically). Failure (key
  missing, or the `EXISTS` call itself errors) → gateway returns
  `{"type":"error","code":"internal_error","client_message_id":...}` for
  `send_message` (mirroring the existing enqueue-failure error shape) and
  the existing `{"type":"error","code":"internal_error"}` shape for
  `mark_*`, without touching Redis — no false "queued"/ack.
- **Not gated:** `edit_message`/`delete_message`/`restore_message`/
  `purge_message` already get a truthful `internal_error` today because
  they synchronously call `/internal/message/*` (ADR 0038) — no change
  needed there. `typing`/`presence_active`/`subscribe_presence` are already
  best-effort/self-healing by design — no change needed there either.

## Consequences

- `send_message` (and receipts) now costs one extra Redis round-trip
  (`EXISTS`) before the `XADD`/ack — negligible next to the existing
  rate-limiter Lua calls on the same path.
- A sender gets an honest, immediate `internal_error` instead of a
  silently-stuck "sent" bubble when the app's workers are down; the PoC's
  existing `internal_error` handling for `send_message` (already present
  for the enqueue-failure branch) covers this without new frontend error
  paths — just a possible copy tweak.
- If Redis itself is down, `EXISTS` fails the same way the enqueue already
  would — no new failure mode.
- Does not fix or attempt to fix delivery of messages already stuck in the
  stream from *before* this change — those still drain normally once the
  app restarts (this only prevents *new* messages from silently entering
  that state).
