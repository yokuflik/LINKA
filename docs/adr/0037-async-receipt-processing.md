# ADR 0037 — Fully async receipt processing

Status: Accepted
Date: 2026-09-10

## Context

`mark_delivered` / `mark_read` / `mark_played` were handled **synchronously** in
the WebSocket receive path (`modules/messaging/receipts.py`): advance the coarse
`Participant.last_*_message_id` watermark, then enqueue the detailed-log row
(already async via `receipt_log_stream`), then run the ADR 0003 read-receipt
privacy gate, then `publish_event` the live `delivery_receipt` / `read_receipt`
/ `played_receipt` to the chat — all before acking the frame, all holding a DB
session per frame.

The Rust WS gateway (ADR 0033) owns the connection but has no DB and must not
grow one (its resource budget is two Redis connections). It therefore cannot do
this work. An `/internal/mark-receipt` HTTP endpoint was considered and
**rejected**: one blocking hop to the single Python process per receipt frame
(receipts are the highest-volume client→server frame — every scroll produces
them) would bottleneck exactly the process we are trying to relieve.

## Decision

Move **all** receipt side effects behind `receipt_log_stream`, mirroring what
FANOUT_REWRITE did for `send_message`:

- **Producers** (`_handle_mark_*` in `realtime/ws_router.py` **and** the Rust
  gateway) do nothing but `XADD receipt_log_stream` the existing 5-field entry
  (`chat_id`, `user_id`, `kind`, `up_to_message_id`, `occurred_at`) and ack.
  No DB, no publish. `modules/messaging/receipts.mark_as_{delivered,read,played}`
  collapse to that single enqueue.
- The **existing** `receipt_log` worker (one consumer group, load already split
  across processes) does everything else, per collapsed `(chat_id, user_id,
  kind)` at the furthest watermark in the batch:
  1. `update_last_{delivered,read,played}_message` — the coarse watermark (drives
     unread count). No-op if already past.
  2. For `played`: skip unless the target message is a voice message (the check
     that used to raise `NotAVoiceMessageError` to the client).
  3. Only if the watermark actually advanced: write the `message_receipt_log`
     row (as before) **and** publish the live receipt event — suppressed for
     `read`/`played` when the reader hid their receipts in a 1:1 (ADR 0003,
     unchanged rule, now evaluated in the worker).
  New logic lives in `modules/receipts/apply.py`; `receipt_log.drain_once`
  orchestrates.

### Behaviour changes (accepted)

- **Receipts are eventually-consistent** (worker latency, sub-second under load).
  The sender's ticks and the reader's unread-count clear lag by that much. The
  client is already optimistic elsewhere; a receipt is not a critical path.
- **A non-voice `mark_played` is silently dropped** instead of erroring. The
  client only ever sends `mark_played` for voice messages, so this is
  unreachable in practice; the worker's type check protects the
  `all_played_up_to_message_id` rollup regardless.
- **The `mark_*` ack no longer means "applied"**, only "queued" — same semantic
  shift `send_message` already made.

### Idempotency / redelivery

`update_last_*` is idempotent (no-op past the watermark). A redelivered batch
re-publishes a receipt event (clients dedupe by `(user, message)`) and can write
a duplicate `message_receipt_log` row — the latter is already true of the
pre-ADR worker and is tolerated (the detailed view dedupes on read).

## Consequences

- One code path for both the Python and Rust WS fleets — a prerequisite for the
  ADR 0033 canary/cutover (RUST_WS_GATEWAY_PLAN.md Step 8).
- `modules/messaging/receipts.py` loses its watermark/publish code; the
  `get_message_receipts` info view is untouched.
- `realtime/ws_router.py` `_handle_mark_*` no longer open a `session_scope`.
- Tests that asserted synchronous receipt effects now drain the worker first
  (most already did).
- The worker does slightly more per batch (a watermark UPDATE + up to one
  `publish_event` per collapsed key, plus one `get_message_by_id` per `played`
  key). Still far below the pre-collapse per-frame cost.

## Alternatives considered

- **`/internal/mark-receipt` HTTP endpoint** — rejected, see Context.
- **A second consumer group on the same stream** — the watermark/publish and the
  detailed-log INSERT act on the same collapsed entries; splitting them doubles
  the reads and complicates ack ownership for no gain.
- **Gateway keeps `mark_*` as a no-op through the canary** — breaks read
  receipts for every Rust-connected user; only acceptable as the pre-ADR
  interim state.
