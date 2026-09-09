# 3. Read-receipts (blue-tick) privacy for 1:1 chats

Date: 2026-08-29

## Status

Accepted

## Context

Read receipts (blue ticks): a user may turn off "read receipts" so the
sender does not see when they read a message. **Group chats are exempt** -
read/played are always recorded and visible there.

We deliberately chose an **asymmetric, per-reader** model (mirroring how
`privacy.online` / last-seen already work here), *not* WhatsApp's symmetric
one: whether user R sends read receipts depends **only on R's own
setting**. The other party's setting is irrelevant to what R sends. So a
sender sees READ/PLAYED for their 1:1 message iff the reader keeps their
own read receipts on.

Our tick model is watermark-derived (`compute_message_status`), plus a
live `read_receipt` / `played_receipt` fan-out event and a detailed
per-message "info" endpoint backed by `message_receipt_log`. All three
surfaces expose READ/PLAYED.

## Decision

- New setting `privacy.read_receipts` (boolean, default `true`) in
  `DEFAULT_USER_SETTINGS` (`services/settings/schema.py`). No migration -
  it lands in the `user_settings.settings` JSONB blob (ADR 0002).
- **Per-reader, asymmetric mask.** In a 1:1 chat (`not chat.is_group`, or
  exactly 2 participants), READ/PLAYED originating from reader R are
  **downgraded to DELIVERED** on every sender-facing surface iff
  `R.privacy.read_receipts = false`. R's own setting is the only input;
  the sender's setting never matters. Group chats (>2 members) are never
  masked.
- **Internal watermarks always advance.** The mask is a
  presentation-layer concern only. `mark_read` / `mark_played` still
  update `Participant.last_read_message_id` etc. and still write the
  detailed log, so the reader's own `unread_count` clears and history is
  intact. Only the *sender-facing* surfaces are masked:
  - `compute_message_status` callers: `services/messaging/read_api.py`
    (`get_message_history` - masks only messages `sender_id == viewer`,
    keyed on the *other* participant's setting) and
    `services/chat_service.py` (`last_message_status`, keyed on the other
    participant's setting).
  - the live fan-out: `mark_as_read` / `mark_as_played` in
    `services/messaging/receipts.py` **skip publishing** the
    `read_receipt` / `played_receipt` event when the acting reader
    (`user_id`) has their own read receipts off in a 1:1 chat (the
    `delivery_receipt` event is unaffected - two grey ticks always show).
  - the detailed `GET /chats/{chat_id}/messages/{message_id}/receipts`
    view: in a 1:1 chat where the sole reader has read receipts off,
    `read`/`played` counts are forced to 0 and `read_by`/`played_by`
    emptied; `pending` then lists the other participant.
- Delivery receipts (DELIVERED, two grey ticks) are never affected.

Helper: `services/messaging/receipt_privacy.py` -
`reader_hides_read_receipts(chat_id, reader_id)` (fan-out) and
`read_receipts_hidden_for_message(chat_id, sender_id)` (read views, keyed
on the *other* participant). 1:1-only; groups short-circuit with zero
settings reads.

## Consequences

- A 1:1 sender whose reader disabled read receipts sees ticks top out at
  DELIVERED (grey). A sender who disabled their *own* read receipts still
  sees the reader's blue ticks - the setting is one-directional.
- The mask is re-evaluated on every read - toggling the setting takes
  effect immediately for new history fetches / events; already-delivered
  live events are not retro-actively recalled.
- Slight extra cost on the read path: one settings lookup for 1:1 chats.
  Groups pay nothing (short-circuit on `is_group`).
