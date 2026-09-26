# 0060 — WS gap detection and targeted resync on dropped fan-out frames

Status: Accepted

## Context

Every outgoing WS frame — chat fan-out (new messages, edits/deletes,
receipts) *and* direct replies (acks/errors) — is enqueued onto a
per-connection bounded `mpsc::channel::<ServerFrame>(32)`
(`crates/ws_gateway/src/ws.rs:135`) via `tx.try_send(...)`
(`ws.rs:364`, `handlers.rs:13`, `fanin.rs:175`). `try_send` is deliberately
non-blocking so one slow client can never stall the writer or the shared
fan-in loop (documented intent at `ws.rs:360-361` and `fanin.rs:171-173`):
a full channel just drops the frame, silently, via `let _ = ...`.

This is the right call for the *server*: a stuck client must not block
fan-in for everyone else. But there is currently no mechanism on the
*protocol* side to recover from it:

- `ServerFrame` carries no sequence number (`state.rs:22-28`), so a client
  cannot detect that a frame was skipped.
- Reconnect only re-runs `GET /internal/ws-bootstrap`
  (`crates/ws_gateway/src/bootstrap.rs`), which returns the current
  `chat_ids` list, not a replay cursor — "self-heals on reconnect" (per
  `bootstrap.rs:5-7`) means the *subscription* is fixed, not that missed
  events are redelivered.
- The dropped frame's underlying data is not lost — `send_message` is
  durably `XADD`ed to `message_send_stream` independent of any live push
  (`send_path.rs`), and `mark_*` similarly durable via `receipt_log_stream`
  (ADR 0037) — so a client that happens to reopen or refresh the affected
  chat will eventually see the correct state via the existing REST history
  endpoint (`modules/messaging/router.py`). But nothing *tells* the client
  it needs to do that. A user with the chat open, or one who never revisits
  it, can miss a live update indefinitely: the new-message bubble, the
  receipt tick, or the unread badge simply never arrives.

## Decision

Add cheap, best-effort **gap detection scoped to chat-carrying frames**,
surfaced to the client via the existing heartbeat cycle, triggering a
**targeted REST refetch** of just the affected chats — not a global
sequence/replay log and not a full resync-on-reconnect.

Rejected alternative: a monotonic `seq` per connection with a
client-acknowledged watermark and server-side replay buffer. Rejected
because it requires persisting a bounded replay window per connection,
reordering/dedup logic on the client, and a new resync handshake — while
the actual data is already durable in Postgres and reachable via a REST
fetch the client already knows how to do. The lighter mechanism below only
needs to tell the client *which chats* to refetch, not replay the missed
bytes itself.

- **Track drops per (connection, chat), not globally.** New
  `AppState.dropped_chats: DashMap<ConnId, HashSet<ChatId>>`. Only frames
  carrying a `chat_id` are tracked — i.e. `fan_out` call sites reached from
  `fanin.rs:112` (chat-scoped `instance_inbox` events) and any future
  chat-scoped broadcast. Presence (`fanin.rs:106`) and direct per-connection
  replies (`ws.rs:364`, `handlers.rs:13`) are **not** tracked: presence is
  inherently ephemeral/self-correcting (re-sent on next status change), and
  direct replies are request-scoped acks the client already retries or
  times out on its own (e.g. `mark_*`'s existing "fire-and-forget: client
  re-sends on next scroll" comment at `ws.rs:351`).
- **Marking:** `fan_out`'s signature changes from
  `fn fan_out(senders: Vec<mpsc::Sender<ServerFrame>>, payload: &str)` to
  additionally take the originating `chat_id: Option<ChatId>` and the
  `AppState`/`ConnId` pairing needed to call `state.mark_dropped(conn_id,
  chat_id)` when `try_send` returns `Err`, for chat-scoped calls only.
- **Delivery to the client:** on every `heartbeat` the client already sends
  periodically, the gateway's existing `heartbeat_ack` reply
  (`ws.rs:397`) is extended: if `state.take_dropped(conn_id)` is non-empty,
  the ack payload gains a `resync_chat_ids` array; the entry set is drained
  (taken) on read so it is only sent once. Piggybacking on `heartbeat_ack`
  avoids spending a second `try_send` slot on a dedicated frame and avoids
  inventing a new client-initiated resync request — if the ack itself gets
  dropped by the same full-buffer condition, the gap flag simply survives
  and rides the next heartbeat.
- **Client behavior:** on receiving `resync_chat_ids` in a
  `heartbeat_ack`, the PoC calls the existing message-history fetch for
  each listed `chat_id` and merges the result into `LinkaChatStore` the
  same way the initial chat-open fetch does (idempotent merge by message
  id — no new merge logic, reuses whatever de-dup the store already does
  for REST-then-WS overlap). No new backend endpoint.
- **Memory bound:** `dropped_chats` entries are removed (`take`) as soon as
  they're flushed to a heartbeat; worst case is O(active chats truly
  affected) per connection between two heartbeats — negligible next to
  `chat_subs`/`user_conns` already held in `AppState`.

## Consequences

- Chat message/edit/delete/receipt fan-out frames dropped under backpressure
  are now recoverable within one heartbeat interval — the client is told
  *which* chats to refetch, closing the "open chat never updates" and
  "unread badge never bumps" gaps described above.
- Presence and direct-reply frames remain best-effort with no gap tracking,
  unchanged from today — deliberately, since re-tracking them would add
  cost for signals that are already either ephemeral or self-retried.
- No wire-format break for existing frame types; `resync_chat_ids` is an
  additive optional field on `heartbeat_ack` — old clients that ignore
  unknown fields are unaffected, they just don't self-heal automatically
  (same as today).
- Adds one small `DashMap` and a `HashSet` per connection with in-flight
  drops; no Redis calls, no new endpoint, no persisted replay buffer.
- Does not fix delivery for a connection that drops without ever sending
  another heartbeat before disconnecting — that case is already covered by
  the client's normal "open a chat -> fetch history" path on its next
  session, same as before this change.
