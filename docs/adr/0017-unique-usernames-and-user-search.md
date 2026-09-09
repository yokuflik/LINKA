# ADR 0017 — Unique lowercase usernames, auto-assigned on signup, exact-match search

Status: Accepted
Date: 2026-09-07

## Context

Users are identified only by `phone_number` (unique) and a free-text
`display_name` (not unique, not a key). We want a stable, unique, shareable
handle — a `username` — so a user can be found and addressed without exposing a
phone number, and so a future "find a user" feature has something safe to query.

Constraints:
- No DB migrations (`init_db.py` `ADD COLUMN IF NOT EXISTS` pattern).
- The `messages`/`users` tables are designed for tens-of-billions scale — any
  search must be a single index point-lookup, never a scan or a prefix/`LIKE`
  probe that an attacker could use to harvest the user table.
- The PoC has no build step and a strict CSP; the client should not need extra
  round-trips during signup.

## Decision

### 1. `username` — required, unique, lowercase-canonical

- New column `users.username VARCHAR(32)`, `UNIQUE`, `NOT NULL`.
- A **plain unique btree index** (`ix_users_username`) is the sole source of
  truth for uniqueness — enforced at the DB, race-safe. No CITEXT extension, no
  functional index: the value is normalised to lowercase on every write, so a
  bare unique index already gives case-insensitive uniqueness.
- Format (validated as untrusted input, every rejection carries a machine
  `reason` code so the client can show a precise hint):
  `^[a-z][a-z0-9_]{2,31}$` — 3–32 chars, starts with a letter, lowercase
  letters / digits / underscore only. Reason codes: `too_short`, `too_long`,
  `bad_chars`, `must_start_letter`, `reserved`, `taken`, `grace_hold`,
  `cooldown`.
- A small reserved list (`admin`, `support`, `linka`, `me`, `null`, `system`,
  `help`, `info`, `root`) is rejected as `reserved`.

### 2. Auto-assigned at account creation

- `auth_service._find_or_create_and_issue` generates a free username for every
  brand-new `User` (`user_service.generate_free_username`, DB/service layer:
  `<adjective>_<noun>_<3–4 digits>` from an embedded word list, fallback
  `user_<base36>`, retried against the unique index, digit count widened on
  collision). `create_user` now takes a required `username`.
- So a user **always** has a username; the client never has to mint one and
  needs no round-trip to get a suggestion — it arrives in the login response.
- The login tuple / `LoginOut` gains `is_new_user: bool`. The frontend uses it
  to open the post-signup "welcome" form (display name / about / avatar), with
  the username field pre-filled from the server-assigned value and a note that
  it can be changed any time. If the user does not touch the field, the random
  username stays (no PATCH is sent).

### 3. Signup flow simplification

- The explicit **"Sign up" toggle is removed** from the PoC auth screen. There
  is one flow: phone → OTP. A previously-unknown phone is find-or-create (it
  already is on the Firebase path).
- The OTP `intent` pre-check is dropped: `request_otp` no longer rejects
  `intent == "login"` for an unknown phone, and the client stops sending
  `intent`. (`register`-already-exists is now a non-case.)

### 4. Changing a username — cooldown + grace hold

> **Superseded in part by ADR 0023.** The single-timestamp cooldown below is
> replaced by a rolling quota: up to `USERNAME_CHANGE_MAX_PER_WINDOW` (3)
> user-initiated changes per rolling `USERNAME_CHANGE_WINDOW_DAYS` (14), tracked
> in a capped `users.username_change_log` JSONB ring. The `reason` code stays
> `cooldown`. `username_changed_at` is still stamped (grace-hold reasoning) but
> is no longer the quota authority. The grace hold below is unchanged.

- `users.username_changed_at TIMESTAMPTZ` (nullable). A change is refused
  (`cooldown`, HTTP 409) until `username_changed_at + USERNAME_CHANGE_COOLDOWN_DAYS`
  (default 14). The initial auto-assignment does **not** set this column, so the
  first user-chosen username is free.
- Releasing a username does not make it instantly grabbable. New table
  `reserved_usernames` (`username` PK lowercase, `reserved_for_user_id`,
  `released_at`, `expires_at = released_at + USERNAME_RESERVED_GRACE_DAYS`,
  default 14). While a row is live (`expires_at > now()`):
  - anyone else is refused that username (`grace_hold`, HTTP 409),
  - the original owner may reclaim it.
  Expiry is checked passively; a prune of dead rows can be bolted onto
  `partition_maintenance.py` later but is not required.
- `set_username` on a *change* (not the initial assignment): stamps
  `username_changed_at`, inserts the old handle into `reserved_usernames`.

### 5. Availability check (advisory)

- `GET /users/username-available?username=` → `{available: bool, reason: str|null}`.
  Format + `reserved_usernames` + `users` + cooldown check. Advisory only — the
  real decision is the unique-index write on `PATCH /users/me`.
- Rate-limited with a dedicated `username_check` bucket (~20 / 60 s / user) so
  it cannot be used to enumerate the table.

### 6. Search — exact match only

- Lookup is **only** by a complete, exact username string
  (`WHERE username = :u` after lowercasing) — a single hit on `ix_users_username`.
- **No `LIKE` / `ILIKE` / prefix / substring / trigram / fuzzy** matching, ever.
  Rationale: a prefix or fuzzy search over a billion-row table is either a scan
  or needs a trigram index that doubles as a scraping tool (feed it `a`, `b`,
  … and page the whole user base). Exact-match keeps it O(1) and useless for
  harvesting — you must already know the handle.
- The result surface is `PublicUserOut` — `id`, `username`, `display_name`,
  `about_text`, `profile_pic_url`, `profile_pic_preview`. **Never
  `phone_number`** (unlike `/users/by-phone`, where the caller already holds the
  number).
- A dedicated `user_search` rate-limit bucket (~15 / 60 s / user, plus the
  existing per-IP REST backstop).
- The actual search **endpoint and UI are deferred** — the placement is still
  to be decided. `crud_user.get_user_by_username` (needed for the availability
  check) is the building block; this ADR fixes the exact-match rule up front so
  the eventual endpoint cannot regress it.

### 7. `UserOut` / live propagation

- `UserOut` gains `username`. `user_service.broadcast_profile_update` and the
  transient `profile_updated` event carry `username` so peers update the cached
  handle without reopening the chat (same mechanism as `display_name` today).

## Consequences

- `username` is `NOT NULL`: `init_db.py` backfills existing rows (generated
  handle per row) before adding the unique index and the `SET NOT NULL`.
- New `reserved_usernames` table; new config knobs `USERNAME_*`
  (`MIN_LEN`, `MAX_LEN`, `REGEX`, `RESERVED`, `CHANGE_COOLDOWN_DAYS`,
  `RESERVED_GRACE_DAYS`, `CHECK_RATE_*`, `SEARCH_RATE_*`).
- Removing the signup toggle / `intent` check means any verified phone creates
  an account — the per-IP `acct_create` cap (ADR 0012 / 0009) is unchanged and
  remains the abuse ceiling.
- Deferred: the search endpoint + PoC "find a user" UI; impersonation of a
  *display name* (only the handle is unique); admin username moderation.
