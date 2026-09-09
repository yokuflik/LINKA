# ADR 0030 — Feature-local API schemas; `api/schemas.py` reduced to shared primitives

**Status:** Accepted
**Date:** 2026-09-09
**Relates to:** ADR 0022 (feature-based module layout)

## Context

`api/schemas.py` held all 34 request/response Pydantic models for every feature
— auth, users, chats, messaging, media, scheduled — in one 453-line file. Every
router imports from it, so a change to a message field forces the OTP and
username models into context, and any schema import drags
`modules.media.media_service.public_avatar_url` (imported at module top) into
scope. This contradicts ADR 0022's "each `modules/<feature>/` owns its own
models/…" principle.

## Decision

Move each model to a `schemas.py` in its owning feature package. `api/schemas.py`
keeps **only genuinely cross-cutting primitives** — currently just `IdStr` (the
Snowflake-as-string annotation used by every module).

| New file | Models |
|---|---|
| `modules/auth/schemas.py` | `OTPRequestIn`, `OTPVerifyIn`, `FirebaseVerifyIn`, `RefreshTokenIn`, `TokenPairOut`, `LoginOut` |
| `modules/users/schemas.py` | `UserOut`, `UserProfileUpdateIn`, `PublicKeyIn`, `PublicKeyOut`, `UserSettingsOut`, `UserSettingsUpdateIn` |
| `modules/media/schemas.py` | `AvatarUploadTicketIn`, `AvatarUploadTicketOut`, `AvatarCommitIn`, `MediaUploadTicketIn`, `MediaUploadTicketOut` |
| `modules/chats/schemas.py` | `ChatOut`, `ChatListItemOut`, `CreatePrivateChatIn`, `CreateGroupChatIn`, `UpdateGroupDetailsIn`, `AddMemberIn`, `ChangeRoleIn`, `MuteChatIn`, `ChatMemberOut`, `ParticipantOut` |
| `modules/messaging/schemas.py` | `MessageOut`, `MessageReceiptEntryOut`, `MessageReceiptsOut`, `ScheduledMediaIn`, `ScheduledMessageIn`, `ScheduledMessagePatchIn`, `ScheduledMessageOut` |

### Cross-module references (the reason for the file boundaries)

- `LoginOut` embeds `UserOut` → `modules/auth/schemas` imports `modules/users/schemas`.
- `ChatMemberOut` embeds `UserOut` → `modules/chats/schemas` imports `modules/users/schemas`.
- `ChatListItemOut` embeds `ChatOut` (same file).
- `MessageReceiptsOut` embeds `MessageReceiptEntryOut` (same file).
- Avatar models are used by **both** `users` and `chats` routers → they live in
  the leaf `modules/media/schemas.py`, which both import.
- `PublicKeyOut` is used by both `users` and `chats` routers → it lives in
  `modules/users/schemas.py`; `modules/chats/router.py` imports it from there
  (chats already depends on users elsewhere).
- `UserOut` / `ChatOut` still import `public_avatar_url` from
  `modules.media.media_service` — unchanged, now scoped to those two files.

**No import cycle:** `api/schemas.py` imports nothing from `modules/`, so the
`modules/*/schemas.py → api.schemas.IdStr` edges are acyclic. `media_service`
does not import any `schemas.py`.

### Compatibility

Only the four `modules/*/router.py` files import `api.schemas`; `tests/` do not
import it at all. The four routers are updated to import from the new locations.
`api/schemas.py` keeps **no** re-export shim (a shim would create the cycle
above and re-bloat the surface). This is a hard move.

## Consequences

- A message-schema change no longer pulls auth/username models into context.
- Each new file is 20–110 lines, all under Rule 9's limit.
- `api/` now holds only `dependencies.py` + a ~20-line `schemas.py`; it could be
  folded into `modules/` entirely in a later pass.
