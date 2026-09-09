# ADR 0023 — Username-change quota (rolling 3 per 14 days)

Status: Accepted
Date: 2026-09-09

Supersedes, in part: ADR 0017 (the "change cooldown" rule only; unique
usernames, auto-assignment, exact-match search and the released-handle grace
hold are unchanged).

## Context

ADR 0017 gave every user a unique handle and guarded changes with a single
14-day **cooldown**: `users.username_changed_at` is stamped on each
user-initiated change, and `user_service._cooldown_active` refuses the next
change until `username_changed_at + USERNAME_CHANGE_COOLDOWN_DAYS`.

The product decision changed: a user should be able to correct a typo or
iterate on a handle a few times, but not churn it continuously (impersonation /
confusion / harassment vector). The wanted rule is **up to 3 changes per
rolling 14 days**, 4th blocked until the oldest of the three ages out.

A single timestamp cannot express "N in a window" — we need the timestamps of
(at least) the last N changes.

Constraints (unchanged): no DB migrations (`init_db.py` `ADD COLUMN IF NOT
EXISTS`), no table scan, minimal surface.

## Decision

### 1. Storage — a capped JSONB timestamp ring on `users`

New nullable column **`users.username_change_log JSONB`**. It holds a JSON
array of ISO-8601 UTC timestamp strings, one per **user-initiated** change,
newest last, hard-capped at `USERNAME_CHANGE_MAX_PER_WINDOW` entries (we only
ever need the oldest of the most recent N). `NULL`/absent is read as `[]`.

On each successful change (`crud.set_username`, `is_initial=False`): drop
entries older than the window, append `now()`, keep the last N, write it back
in the same `UPDATE ... RETURNING`. `username_changed_at` is still stamped —
kept for the grace-hold reasoning and any external reader — but is no longer
the quota authority.

Rejected alternatives:
- **Reuse `reserved_usernames` as a change log.** Its `username` column is the
  PRIMARY KEY; a user cycling A→B→A→C within the window collides on the second
  release of "A" and the whole change fails. Not a trustworthy counter.
- **Dedicated `username_change_events` audit table.** More power (history,
  forensics) but a new table + index + prune job for a feature that only needs
  a count. Noted as the upgrade path if abuse forensics are ever needed.

### 2. Quota check

`user_service._change_quota_exceeded(user)` replaces `_cooldown_active`:
parse `username_change_log`, keep entries newer than
`USERNAME_CHANGE_WINDOW_DAYS`, and if the count is
`>= USERNAME_CHANGE_MAX_PER_WINDOW` return the ISO timestamp
`oldest_recent + window` (when the quota frees up); else `None`. Same two call
sites as before (`set_username` hard-reject 409, `check_username_available`
advisory).

### 3. Reason code unchanged

The machine `reason` stays **`cooldown`** — the client already maps it to a
hint and renaming it would ripple into the PoC and tests for no user benefit.
Only the human hint text changes ("changed it a few times recently").

### 4. Config (`config/username_settings.py`)

`USERNAME_CHANGE_COOLDOWN_DAYS` (14) is **removed**, replaced by:
- `USERNAME_CHANGE_WINDOW_DAYS` (default 14)
- `USERNAME_CHANGE_MAX_PER_WINDOW` (default 3)

Both env-overridable.

### 5. No migration; existing users

`ALTER TABLE users ADD COLUMN IF NOT EXISTS username_change_log JSONB` in
`init_db.py`. Existing rows read as `NULL` → `[]` → their next 3 changes are
free. The old single-timestamp cooldown is **abandoned, not migrated** — a user
currently "in cooldown" immediately regains 3 changes. This is intended.

## Consequences

- One extra JSONB column (≤3 short strings, ~90 bytes) on a row already fetched
  by `get_user_by_id`. No new table, no new index, no new query.
- The quota is best-effort against a determined actor with many accounts — same
  threat posture as ADR 0017; out of scope here.
- No audit trail of past handles beyond the 14-day window. If needed later, add
  `username_change_events` without touching this design.

## Companion fixes (same change)

Two pre-existing bugs in the change path, exposed while testing the quota:

1. **`reserved_usernames` PK collision on handle cycling.** `crud.set_username`
   did `session.add(ReservedUsername(...))` for the released handle. A user
   cycling A→B→A→C re-releases "A" and hits the `username` PRIMARY KEY — the
   whole `UPDATE` aborts and surfaces as a bogus `taken`. Fixed with an
   `INSERT ... ON CONFLICT (username) DO UPDATE` (refresh owner + expiry).
2. **Reclaiming your own grace-held handle left a stale reservation.** When a
   change targets a handle sitting in the caller's own grace hold, that
   `reserved_usernames` row is now deleted (scoped to
   `reserved_for_user_id = caller`; a hold owned by another user is already
   rejected upstream as `grace_hold`).

Frontend: the PoC `profile_updated` WS handler updated `userById` and group
member rows but not `privateChatTitles` — the denormalised string the private
sidebar row + chat header actually render — so a peer's live username change
didn't show until reload. Now refreshed in the handler when
`privateChatOtherUserId[chat_id]` matches the event's user.

## Affected code

- `config/username_settings.py` — swap the two settings + `__all__` + docstring.
- `modules/users/models.py` — add `username_change_log` column.
- `scripts/init_db.py` — `ADD COLUMN IF NOT EXISTS`.
- `modules/users/crud.py` `set_username` — maintain the ring; `ReservedUsername`
  upsert; delete own stale grace-hold row on reclaim.
- `modules/users/service.py` — `_cooldown_active` → `_change_quota_exceeded`.
- `poc/composables/useProfileEdit.js` — hint text.
- `poc/composables/useWsRouter.js` — refresh `privateChatTitles` on
  `profile_updated`.
- `.claude_docs/backend_services_and_api.md`, ADR 0017 note, CLAUDE.md index.
- `tests/modules/users/test_user_service.py`.
