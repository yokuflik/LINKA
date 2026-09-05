# ADR 0013 — Split `chat_service.py` into a `services/chats/` domain package

Status: Accepted
Date: 2026-09-06

## Context

`services/chat_service.py` had grown to 534 lines spanning several unrelated
sub-domains: idempotent 1:1 / group creation, the home-screen chat list, the
member list, per-user pin/mute preferences, group profile edits, and
add/remove/re-role membership management — plus their shared helpers
(`_require_role`, `_display_name_for`) and the live-nudge publishers
(`_notify_added_to_chat`, `_notify_removed_from_chat`, `_broadcast_chat_update`).

This is the same shape of problem `message_service.py` had before it was split
into `services/messaging/` with a thin re-export facade (no ADR was recorded for
that one; this ADR covers the pattern for both going forward).

CLAUDE.md Rule 9 flags files over ~300 lines. Rule 7 requires an ADR before a
significant structural change.

## Decision

Split the implementation by responsibility into a new `services/chats/`
package, and keep `services/chat_service.py` as a **thin facade** that
re-exports the public API. Every existing importer (`routers/chats.py`,
`routers/messages.py`, `routers/websocket.py`, other services, tests) keeps
`from services import chat_service` / `from services.chat_service import …`
unchanged.

### Modules

| Module | Public surface |
|---|---|
| `errors.py` | `PermissionDeniedError`, `TooManyMembersError`, `UserNotFoundError`, `OwnershipTransferRequiredError` |
| `common.py` | `ROLE_MEMBER/ADMIN/OWNER`, `_require_role`, `_display_name_for` |
| `notifications.py` | `_notify_added_to_chat`, `_notify_removed_from_chat`, `_broadcast_chat_update` |
| `creation.py` | `get_or_create_private_chat`, `create_group_chat` |
| `listing.py` | `get_chat_list`, `get_chat_members` |
| `preferences.py` | `set_chat_pinned`, `set_chat_muted` |
| `group_details.py` | `update_group_details`, `ensure_can_manage_details`, `set_group_avatar`, `clear_group_avatar` |
| `membership.py` | `add_member`, `remove_member`, `change_member_role` |

### Dependency graph (one-way, no cycles)

```
errors, common
   ↑
notifications ── (calls chat_service.realtime_service at call time)
   ↑
creation, group_details, membership
listing, preferences ── (common + crud only)
   ↑
chat_service.py  (facade: re-exports everything)
```

### Monkeypatch compatibility

Two names tests patch on the facade are read back off it at call time by the
submodules (identical to the `message_service` facade's
`MAX_MESSAGE_CONTENT_LENGTH` handling):

- `chat_service.MAX_INITIAL_GROUP_MEMBERS` → `creation.create_group_chat` does
  `from services import chat_service; chat_service.MAX_INITIAL_GROUP_MEMBERS`.
- `chat_service.realtime_service` → `notifications.py` and `preferences.py`
  reference `chat_service.realtime_service.publish_*` at call time, so
  `monkeypatch.setattr(chat_service.realtime_service, "publish_event", …)`
  still intercepts.

The facade also keeps `_require_role` / `_display_name_for` / the `_notify_*` /
`ROLE_*` importable, since existing tests reach for `chat_service.ROLE_OWNER`
etc.

## Consequences

- Pure code move; **zero behaviour change**. No schema, no API, no wire change.
- Each module is now well under the 300-line guideline.
- New chat-domain code goes in the relevant submodule; the facade only grows a
  one-line re-export when a genuinely new public function is added.
- Slight import indirection cost (the call-time `from services import
  chat_service` inside `creation` / `notifications` / `preferences`) — the
  price of preserving the existing monkeypatch surface, same trade-off already
  accepted for `messaging`.
