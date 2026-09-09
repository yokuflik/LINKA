# ADR 0025 — Foreground-only presence (client-driven active/inactive)

Status: Accepted
Date: 2026-09-09

## Context

`presence_service` already documents "online = an open, **foreground** WebSocket
connection" (WhatsApp semantics), but nothing enforced the *foreground* part: the
presence key was added on WS connect and only removed on disconnect. A user with
the tab open in the background, behind another window, or on another tab still
showed as **online** and their `typing` / `recording` indicators still fanned
out.

The read-receipt path solved the same problem on the client
(`useChatOpen.windowIsActive()` — `document.visibilityState === 'visible'` plus
`document.hasFocus()` on desktop). We want presence and the typing indicator to
follow the exact same rule.

Separately: when the client's own socket is down, it kept rendering the peer's
last-known `online` / `typing` state, which is stale and misleading.

## Decision

### Backend — new WS action `presence_active`

- **`presence_active {active: bool}`** — the client asserts whether *this*
  connection is currently foreground. `_handle_presence_active` →
  `presence_service.set_active(user_id, connection_id, SERVER_ID, active)`:
  - `active: true`  → `mark_online(...)` (idempotent `sadd` + TTL refresh +
    `presence_update {online}` on the 0→1 edge).
  - `active: false` → `mark_offline(...)` (`srem` + `presence_update {offline,
    last_seen_at}` on the →0 edge).
  - `set_active` is a thin wrapper over the two existing functions — the
    edge-triggered publish, multi-device set semantics and `last_seen` stamping
    are unchanged.
- The presence key set now means **"foreground connections"**, not "all
  connections". Multi-device: online iff ≥1 connection is foreground.
- A fresh WS connect still calls `mark_online` (a connection is assumed
  foreground at open); the client immediately sends `presence_active {false}` if
  it opened hidden, and re-asserts the current state on every reconnect.
- `heartbeat` still refreshes the TTL for whatever is in the set; a
  backgrounded (already `srem`-ed) connection's heartbeat is a harmless no-op on
  the key. `heartbeat` keeps stamping `last_seen` (the tab *is* open).
- Rate limit: shares the `ws_sub_presence` bucket
  (`WS_SUBSCRIBE_PRESENCE_RATE_MAX` 20 / 10 s) — presence-related and
  visibility flaps are naturally bounded; the client also de-dupes (only sends
  on an actual state change).
- No DB change, no config change, no new event type (reuses `presence_update`).

### Frontend

- **`usePresence.syncPresenceActive()`** — computes `ctx.windowIsActive()` and
  sends `presence_active` only when the value changed since the last send.
  Wired to `visibilitychange` + `window` `focus` / `blur`, and re-armed +
  re-sent from `resubscribePresenceForActiveChat()` (already called on
  `ws.onopen`).
- **`useTyping.notifyTyping()` / `notifyRecording()`** — early-return unless
  `ctx.windowIsActive()`, so a backgrounded tab never emits the indicator
  (mirrors `markActiveChatReadIfVisible`).
- **Stale-state hiding while the socket is down:** `activeChatPresenceLabel`,
  `activeChatTypingLabel` and `typingLabelForChat()` return `''` when
  `ctx.wsStatus.value !== 'connected'`. `ws.onclose` also calls `resetTyping()`
  / `resetPresence()` so nothing lingers on reconnect before the fresh
  `presence_status` pull.

## Consequences

- Presence and the typing indicator now match read-receipt visibility rules
  exactly — backgrounding the tab drops you offline within ~one heartbeat
  (immediately on the `blur`/`visibilitychange` send; the 60 s TTL is only the
  crash fallback).
- `is_online` / `get_online_participants` (used by fan-out to pick live delivery
  vs push) now also mean "foreground" — a backgrounded user gets a push
  notification instead of a silent live fan-out. This is the intended behaviour.
- Desktop `document.hasFocus()` flaps (alt-tab) cause an online/offline churn
  bounded by the `ws_sub_presence` limiter; acceptable.
- Older clients that never send `presence_active` behave exactly as before
  (online while connected).
