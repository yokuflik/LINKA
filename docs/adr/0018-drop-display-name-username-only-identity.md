# ADR 0018 — Drop `display_name`; identify users by `username` only

Status: Accepted
Date: 2026-09-07

## Context

Since ADR 0017 every account carries a unique, shareable `username`. The older
free-text `display_name` (nullable `String(50)`) is now redundant: it is a
second, non-unique name field that the client has to fall back through
(`display_name || phone_number`), it can impersonate other users, and it adds a
field to every profile form, the signup welcome step, the `UserOut` schema and
the `profile_updated` fan-out.

The product decision: there is **no display name and no separate "about"
concept split from it** — a user is shown by their `username`, falling back to
`phone_number` when a username is somehow absent. The `about_text` bio line is
**kept** at every level (user profile + group description).

There is already no client-side contact book (removed before ADR 0017); "resolve
a name" has always meant "read the server's name field".

## Decision

- **Remove `users.display_name`** entirely: the model column, `crud_user`
  (`create_user` / `update_user_profile` params), `user_service.update_profile`,
  `UserOut`, `UserProfileUpdateIn`, and the `profile_updated` WS payload.
- No migration (project rule). The dev DB is recreated by `init_db`; a
  deployed DB keeps the now-unreferenced column until a future consolidation —
  SQLAlchemy simply stops selecting/writing it.
- **Peer display everywhere = `username || phone_number`.** Updated in the PoC:
  `useChatMembers` (`resolvePrivateChatTitle`, `senderLabel`, `userLabelById`,
  `memberDisplayName`, `currentUserNameVariants`, draft-chat computeds),
  `AppHeader`, `useWsRouter` `profile_updated`, `useProfileEdit`, `useAuth`
  welcome flow, `AuthScreen` (welcome pane loses the "Display name" input),
  `ProfileEditModal` (generic name field now opt-in via `nameKey`; the user
  modal shows only Username + About + avatar, the group modal still passes
  `nameKey="title"`).
- **System-message text** (`services/chats/common._display_name_for`, name
  unchanged) now resolves `username or phone_number`.
- `about_text` unchanged at all levels.
- Mock seed data regenerated: `USERS` is `(phone, username)` pairs; no backfill
  step.

## Consequences

- One name field, always unique. No `display_name || …` fallbacks.
- Existing rows in a deployed DB keep a dead `display_name` column (harmless).
- Tests that set/assert `display_name` (`test_crud_user`, `test_user_service`,
  `test_rest_api`) updated to drop it.
