# 2. Per-user settings as a single JSONB blob

Date: 2026-08-28

## Status

Accepted

## Context

Users need persistent, per-user settings. The first is a privacy control
("who may see my presence" — one of `everyone` / `contacts` / `nobody`),
but many more will follow
(notifications, security, chat preferences, ...). We have no migration
tooling (schema changes are code + a manual `ALTER TABLE IF NOT EXISTS`),
so a design that needs a new column per setting is a recurring tax.

## Decision

- New table `user_settings`: `user_id` PK/FK (`ON DELETE CASCADE`), a
  single `settings JSONB NOT NULL DEFAULT '{}'`, `updated_at`. 1:1 with
  `users`, row created lazily on first write.
- Stored blob is **sparse** — only keys the user explicitly changed. The
  canonical shape and defaults live in
  `services/settings/schema.py::DEFAULT_USER_SETTINGS`; reads deep-merge
  the stored blob over a copy of the defaults.
- Writes are **partial patches**, validated server-side against the
  canonical shape: any unknown group/key or out-of-enum value is rejected
  (`SettingsValidationError` → HTTP 400). Clients cannot put arbitrary
  JSON into the blob.
- Settings are grouped one level deep (`privacy`, later `notifications`,
  ...). Adding a setting = extend `DEFAULT_USER_SETTINGS` (+ `_ENUMS` for
  a constrained value). No migration, no new column, no API change.
- Default privacy visibility is `everyone` for every field.
- API: `GET /users/me/settings`, `PATCH /users/me/settings`
  (`{ "settings": { ...partial... } }`), both returning the fully
  resolved settings.

## Presence enforcement (`privacy.online`)

`privacy.online` is the **single** presence-privacy control: it gates the
live online/connected indicator *and* the "last seen" timestamp together —
they are one signal. (An earlier draft had a separate `privacy.last_seen`;
it was removed — last-seen only ever ships over the same
`privacy.online`-gated `subscribe_presence` channel, so a second knob was
dead weight. `merge_with_defaults` prunes the retired key from any stale
stored blob.)

`_handle_subscribe_presence` (`routers/websocket.py`) resolves the target
user's `privacy.online` at subscribe time via
`settings_service.get_online_visibility`:

- `nobody` → `forbidden`, no subscription, no `presence_status` pull.
- `contacts` → allowed only when a `PrivateChatPair` between the two
  users exists (`get_pair_chat_id`) — "someone I have a chat with".
- `everyone` (default) → allowed for any authenticated user.

This **replaced** the previous unconditional rule ("a private chat pair
must exist"), which is now only the `contacts` branch.

The gate runs **only at subscribe time, never on each presence push** — a
popular user can have thousands of watchers and every connect/disconnect
must not fan out a DB authorization check. `presence_service` keeps
publishing unconditionally; a client with no live subscription simply
receives nothing.

To make a *later* privacy change take effect without a per-push check or
a broadcast of revokes: the client re-sends `subscribe_presence` for its
open chat on every heartbeat (~30s). `_handle_subscribe_presence` re-runs
`_presence_authorized`; a watcher that is no longer allowed is
unsubscribed server-side and sent `{type:"presence_revoked", user_id}`,
which the client uses to drop the cached "online". Worst-case staleness
is one heartbeat interval — acceptable for this signal, and O(1) per
watcher with no extra server state.

Last-seen rides this same gate: `get_status` returns `last_seen_at` on the
subscribe pull and every `presence_update`, all of which only reach an
already-authorized watcher.

## Consequences
- No per-key indexing/querying across users (not needed; settings are
  always read for one known user).
- The blob is unbounded in principle; in practice it is a handful of
  small scalars per user.
