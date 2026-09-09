# ADR 0028 — Per-user hard storage quota

Status: Accepted
Date: 2026-09-09

## Context

Media objects are content-addressed and deduped globally (ADR 0010): the app
holds a `media_blob` row per distinct file with a `ref_count`, and stores the
bytes in the private media bucket exactly once. There is **no cap** on how much
a single user can push into storage. On the live single-host demo
(`docs/adr/0007`, real AWS S3 per ADR 0008) one user uploading large files in a
loop is an unbounded cost and a trivial abuse vector.

Requirement: cap each user at a fixed amount of file storage. Past the cap,
their uploads are blocked until they free space by deleting files from the
server ("delete forever" / purge — ADR 0021, the only path that removes bytes).

## Decision

A **hard per-user quota** of `STORAGE_QUOTA_BYTES` (default **1 GiB**),
enforced at the point a media upload ticket is minted.

### Counting model — per **ref**, not per physical object

`users.storage_bytes_used` is a running total of the sizes of the media
messages a user has **sent**, counted once per send. Dedup is ignored for the
quota: if user A forwards a 10 MB video that is already in storage, A's counter
still goes up by 10 MB. Rationale:

- The quota is then predictable and stable for the user — "your files" is the
  sum of what you sent, regardless of what anyone else uploaded.
- No `user × blob` join table, no walking `ref_count` owners.
- Matches how the user perceives it (the file is "in their chat").

Trade-off: the number does not reflect true bytes-at-rest, so it is a fairness
cap, not a billing figure. Acceptable — this is abuse protection, not metering.

### DB

- New column **`users.storage_bytes_used BIGINT NOT NULL DEFAULT 0`**. No
  migration — `ALTER TABLE users ADD COLUMN IF NOT EXISTS storage_bytes_used
  BIGINT NOT NULL DEFAULT 0` in `init_db.py` (non-`--drop`).
- **Existing users start at 0.** No backfill from historical `messages`. The
  quota applies to uploads from this point forward; pre-existing media is free.

### Enforcement point — the upload-ticket endpoint

`POST /chats/{chat_id}/messages/upload-ticket` (request path, `modules/messaging/router.py`):

- Read `users.storage_bytes_used`. If
  `used + body.size_bytes > STORAGE_QUOTA_BYTES` → raise
  `StorageQuotaExceededError` → **HTTP 413** with
  `{"detail": "...", "reason": "storage_quota_exceeded"}`.
- This is checked **before** the dedup branch — a deduped
  (`already_uploaded`) send still consumes quota because it still becomes a ref
  the user holds.

The WebSocket send path is **not** the enforcement point: it runs in the
fan-out worker, off the request path, so a rejection there could not reach the
client cleanly. The ticket endpoint is the natural gate — no ticket, no
upload, no send.

### Counter maintenance

- **+`media_size`** on confirmed send: `media_validation._validate_media`
  already HEAD-verifies the object and calls `confirm_and_ref`; it now also
  calls `crud_user.add_storage_usage(session, sender_id, +size)`. Every send
  (including deduped re-sends / forwards) increments.
- **−`media_size`** on purge: `edit_delete.purge_message` (ADR 0021) already
  derefs the blob; it now also calls
  `crud_user.add_storage_usage(session, original_sender_id, -size)` using the
  pre-purge `media_size` / `sender_id` captured before the row is wiped. The
  counter is floored at 0 (`GREATEST(storage_bytes_used - :n, 0)`).
- Plain soft-delete does **not** refund quota — the bytes are still in storage
  and the message is restorable. Only an irreversible purge frees space, which
  is exactly the "delete files to make room" behaviour we want.

### TOCTOU

The check (ticket) and the increment (send) are not atomic — a user could pull
several tickets in parallel before sending, briefly overshooting the cap. For a
demo hard-cap this is acceptable: the overshoot is bounded by the per-kind
upload ceiling × the parallel-ticket count, and the next ticket request is
refused. Not worth a reservation table.

### Config

`STORAGE_QUOTA_BYTES` in `config/storage_settings.py`, env-overridable,
default `1 * 1024**3`.

### Frontend (PoC)

`useMediaUpload.js` catches the `413` from the ticket call and shows a polite
toast — "Storage full — delete some files to upload more." — never the raw
error (per the frontend error-UX rule). No quota meter UI.

## Consequences

- First per-user resource cap in the system. Bounds the demo's S3 bill and
  kills the loop-upload abuse vector.
- The counter is an approximation (per-ref, not per-object); documented as a
  fairness cap, not metering.
- No retroactive effect on existing users' stored media.
- Still no general storage GC (orphan sweeper, account-deletion cascade) — out
  of scope, unchanged from ADR 0010 / 0021.
