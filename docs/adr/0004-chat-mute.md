# 4. Per-user chat mute as a `muted_until` timestamp on Participant

Date: 2026-08-29

## Status

Accepted

## Context

Users want to mute a chat for a bounded period (8 hours, 1 day, 1 week) or
indefinitely. Muting is overwhelmingly a **client presentation** concern:
the muted chat still receives messages, the client just suppresses the
notification / unread badge for it. The one place the server genuinely
must act is the offline push path — a user who muted a chat and closed the
app would otherwise still get FCM pushes from it, defeating the feature.

The mute state is per **(user, chat)**, not global per user, so it does
not belong in the `user_settings` JSONB blob (ADR 0002). It is the same
shape as per-user pinning (`Participant.pinned_at`).

## Decision

- New nullable column `Participant.muted_until TIMESTAMPTZ`. `NULL` = not
  muted. A timestamp in the future = muted until then. "Mute forever" is
  just a far-future timestamp (client sends e.g. year 9999) — the server
  has no separate "forever" concept.
- **The client owns the durations.** The server accepts any `muted_until`
  timestamp and stores it verbatim. The 8h / 1d / 1w / forever menu is a
  client-side choice; adding or changing a duration needs no server change.
- **Server enforcement is limited to the offline push path**
  (`services/messaging/send.py::fan_out_message`): recipients whose
  `muted_until > now()` are dropped from `offline_ids` before
  `notification_service.send_push`. This is a single filter, not
  duration logic. Real-time fan-out to connected clients is unchanged —
  the client filters those.
- Expiry is passive: no cron, no cleanup. A past `muted_until` simply
  stops matching `> now()`. The client re-reads it from `GET /chats` and
  un-mutes its own UI when it lapses.
- Endpoints, mirroring pin: `PUT /chats/{chat_id}/mute`
  (`{ "muted_until": <ISO 8601> }`) and `DELETE /chats/{chat_id}/mute`.
  Both 204, or 404 if the caller is not a participant. No role check —
  muting is a personal chat-list preference.
- Multi-device sync: on success, publish
  `{ event: "chat_mute_changed", chat_id, muted_until }` on the acting
  user's `user_events:{user_id}` channel (same mechanism as
  `chat_pin_changed`), so the user's other devices update live.
- Exposed as `ChatListItemOut.muted_until` (Optional), set only by
  `chat_service.get_chat_list` from `Participant.muted_until`.

## Consequences

- No server-side "is this chat muted" is consulted anywhere except the
  offline push filter — deliberately. Everything else about muting is the
  client's job.
- Column added via `ALTER TABLE participants ADD COLUMN IF NOT EXISTS
  muted_until TIMESTAMPTZ` in `init_db.py` (no migrations — CLAUDE.md).
- A clock-skewed client could send a slightly-off expiry; harmless for
  this signal.
