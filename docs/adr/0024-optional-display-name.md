# ADR 0024 — Optional free-form `display_name` as a presentation layer over `username`

Status: Accepted
Date: 2026-09-09

## Context

ADR 0018 removed `users.display_name` and made the unique `username` (ADR 0017)
the single identity. The product now wants a friendly, multi-language nickname
back: a name a user can set in any script (Hebrew, Arabic, CJK, emoji), with no
uniqueness and only a length cap.

The reason 0018 dropped it still stands — a free-form name is an **impersonation
vector** ("Linka Support", or a copy of a friend's handle). This ADR reintroduces
it deliberately, as a *display* layer that never replaces the unique `username`
and never becomes searchable.

## Decision

- **New column `users.display_name` — nullable `String(80)`.** No uniqueness, no
  index. `NULL`/absent = the user has no nickname.
- **No migration** (project rule). Fresh dev DBs get it from `create_all`;
  `scripts/init_db.py` adds `ALTER TABLE users ADD COLUMN IF NOT EXISTS
  display_name VARCHAR(80)` for already-initialised dev DBs; a deployed DB needs
  the same `ALTER` run once by hand (same as ADR 0021's `purged_at`).
- **Sanitised as untrusted input** in `user_service.sanitize_display_name`:
  trim, strip C0/C1 control chars, strip Unicode bidi / directional-override /
  zero-width code points (`U+200B–200F`, `U+202A–202E`, `U+2066–2069`,
  `U+FEFF`), `unicodedata.normalize("NFC", …)`, then truncate to
  `DISPLAY_NAME_MAX_LEN` (50) code points. Result `""` → stored as `NULL`.
  No HTML stripping needed (the PoC escapes; there is no other renderer).
- **Fallback rule everywhere a person is shown:**
  `display_name || username || phone_number`.
- **Peer display shows only the chosen name.** When another person has a
  `display_name` the UI shows just that — the underlying `@username` is *not*
  rendered next to it (product choice). The unique handle is still reachable in
  the peer's full profile and in the New-chat lookups; the anti-impersonation
  guarantees below rest on `username` staying unique + non-searchable
  `display_name` + unchanged system-message text, not on a visible handle.
  (`useChatMembers.peerHandle` / `chatSubLabel` / `activeChatSubLabel` exist as
  hooks but currently return empty.)
- **System-message text is NOT changed** — `modules/chats/common._display_name_for`
  stays `username or phone_number`. System lines must name a non-spoofable
  identity.
- **Search is NOT changed** — still exact-match on `username` only (ADR 0017).
  `display_name` is never matchable by prefix, substring, LIKE or trigram.
- **API surface:** `UserOut` gains `display_name`; `UserProfileUpdateIn` gains
  `display_name` (a *sent* field — present in `model_fields_set` — sets or, when
  `""`/`null`, clears it; an absent field leaves it untouched);
  `PATCH /users/me` routes it through `sanitize_display_name`; the transient
  `profile_updated` WS event carries `display_name`.
- **Sign-up welcome flow:** an optional "Display name" input next to the
  username field (`useAuth` / `AuthScreen`), sent in the same best-effort
  `PATCH /users/me`.
- Mock seed (`scripts/seed_mock_data.py`) regenerated as `(phone, username,
  display_name)` triples — some users left with `display_name=None` to exercise
  the fallback, some with multi-language nicknames.

## Consequences

- Partially reverses ADR 0018 (`display_name` is back, but as an optional
  presentation field, not an identity).
- One more nullable column on `users`; deployed DBs need the manual `ALTER`.
- No new enumeration surface (no index, no search path).
- `about_text` and the `username` identity/quota model are untouched.
- Tests updated: `test_crud_user`, `test_user_service` (sanitisation:
  bidi/control/zero-width/NFC/cap/empty→None), `test_rest_api` (PATCH with /
  without / clearing the field; `UserOut` shape), plus a `broadcast_profile_update`
  payload assertion.
